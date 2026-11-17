"""
Вкладка CS3BET: мини-апп со ставками на КС (виртуальные монеты).

Для каждого аккаунта панели:
  - через Telethon берём init data мини-аппа бота и логинимся в API (токен кэшируется);
  - баланс  -> GET  /api/profile
  - матчи   -> GET  /api/matches/active (+ /api/matches/{id}/detail за свежими коэффициентами)
  - экспресс-> POST /api/bets/express
  - одиночная ставка -> POST BET_SINGLE_PATH (см. ниже, путь нужно сверить с реальным API)

Эндпоинты панели:
  GET  /api/cs3bet/balance/{acc}
  GET  /api/cs3bet/matches/{acc}
  POST /api/cs3bet/bet/{acc}     {stake | "all", legs: [{match_id, side: "1"|"2", odds?}]}
  GET  /api/cs3bet/rewards/{acc}          таймеры ежедневной награды и колеса
  POST /api/cs3bet/rewards/{acc}/collect  {daily, wheel} забрать созревшее
  POST /api/cs3bet/promo/{acc}   {code}  -> /api/promocodes/redeem
  GET/POST /api/cs3bet/template  шаблон ставки (файл cs3bet_template.json)
  GET  /api/cs3bet/state         всё, что фоновый обходчик успел собрать по аккаунтам (файл cs3bet_state.json)

Фоновый обходчик: берёт аккаунты по очереди, у каждого получает баланс, активные ставки, ежедневную награду
и колесо (созревшее забирает), сохраняет в файл и едет к следующему. При 429 ждёт 10 сек и повторяет
(ещё 429 — ещё 10 сек). После последнего аккаунта начинает заново. Пока идёт ставка / загрузка матчей /
запрос баланса от пользователя, обходчик стоит — приоритет у пользователя.
"""
import asyncio
import collections
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import web
from cs3bet_bulk import BulkGame, MAX_STAKE, MAX_ODDS

BOT_USERNAME = os.getenv("CS3_BOT_USERNAME", "cs3bet_bot").strip().lstrip("@")
API_BASE = os.getenv("CS3_API_BASE", "https://app.cs3newsbet.com").rstrip("/")
# ВНИМАНИЕ: в cs3.py есть только /api/bets/express. Путь одиночной ставки ниже — предположение,
# его нужно сверить с реальным API (поправь тут или задай переменной окружения CS3_SINGLE_PATH).
BET_SINGLE_PATH = os.getenv("CS3_SINGLE_PATH", "/api/bets/single")
BET_EXPRESS_PATH = "/api/bets/express"
SINGLE_CANDIDATES = list(dict.fromkeys([BET_SINGLE_PATH, "/api/bets", "/api/bets/place", "/api/bets/ordinary", "/api/bets/regular"]))
MIN_STAKE = Decimal("100")
TEMPLATE_FILE = Path(__file__).resolve().parent / "cs3bet_template.json"
RATE_MAX = int(os.getenv("CS3_RATE_MAX", "50"))     # макс. запросов к API за окно (лимит сервера ~60/мин)
RATE_WINDOW = 60.0
TOKEN_TTL = 20 * 60          # сек, потом логинимся заново
STATE_FILE = Path(__file__).resolve().parent / "cs3bet_state.json"
PAUSE_429 = 10               # сек паузы при 429 в фоновом обходе (повторяется, пока не пройдёт)
MAX_429_TRIES = 8            # после стольких подряд 429 аккаунт пропускаем до следующего круга
USER_RESERVE = 12            # сколько запросов в окне оставляем под действия пользователя
CYCLE_GAP = 3                # пауза между кругами обхода, сек

class RateLimited(Exception):
    def __init__(self, retry_after=0, flood=False):
        super().__init__("rate limited")
        self.retry_after = int(retry_after or 0)
        self.flood = flood       # True — это FloodWait от Telegram, а не 429 от API


_session: aiohttp.ClientSession | None = None
_tokens: dict[str, tuple[str, float]] = {}
_login_locks: dict[str, asyncio.Lock] = {}
_bet_locks: dict[str, asyncio.Lock] = {}
_calls = collections.deque()
_blocked_until = 0.0            # после 429 от API все запросы ждут до этого момента (monotonic)
_prio = 0                       # сколько пользовательских действий (ставка, матчи, баланс) идёт прямо сейчас
_prio_until = 0.0
_rate_lock = asyncio.Lock()
_rw_locks: dict[str, asyncio.Lock] = {}
_tg_sem = asyncio.Semaphore(3)   # не долбим Telegram логинами разом со всех аккаунтов


def _lock(store, key):
    if key not in store:
        store[key] = asyncio.Lock()
    return store[key]


def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
    return _session


def nested(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "."))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_dicts(value)


def extract_coefficients(match, detail):
    """Из cs3.py: поиск коэффициентов П1/П2 в ответах API."""
    sources = [detail, match]
    team_a = match.get("team_a_name") or nested(detail, "team_a_name")
    team_b = match.get("team_b_name") or nested(detail, "team_b_name")
    found = {"1": None, "2": None}
    direct_pairs = [
        ("team_a_coefficient", "team_b_coefficient"), ("team_a_odds", "team_b_odds"),
        ("team_a_coef", "team_b_coef"), ("odds_team_a", "odds_team_b"),
        ("coefficient_a", "coefficient_b"), ("home_odds", "away_odds"),
        ("p1_odds", "p2_odds"), ("p1_coefficient", "p2_coefficient"),
    ]
    for source in sources:
        for node in walk_dicts(source):
            for a_key, b_key in direct_pairs:
                if a_key in node and b_key in node:
                    a, b = numeric(node.get(a_key)), numeric(node.get(b_key))
                    if a and b:
                        return a, b
    coefficient_keys = ("coefficient", "odds", "price", "value", "odd", "coefficient_at_bet")
    for source in sources:
        for node in walk_dicts(source):
            raw = next((node.get(k) for k in coefficient_keys if k in node), None)
            coeff = numeric(raw)
            if not coeff:
                continue
            outcome_id = str(node.get("outcome_external_id", node.get("outcome_id", node.get("external_id", node.get("id", ""))))).lower()
            label = " ".join(str(node.get(k, "")) for k in ("name", "label", "title", "outcome_name", "team_name", "display_name")).lower()
            if outcome_id in {"1", "home", "p1", "team_a"} or (team_a and team_a.lower() in label):
                found["1"] = coeff
            elif outcome_id in {"2", "away", "p2", "team_b"} or (team_b and team_b.lower() in label):
                found["2"] = coeff
    if found["1"] is None or found["2"] is None:
        team_a_id = str(match.get("team_a_id", nested(detail, "team_a_id") or ""))
        team_b_id = str(match.get("team_b_id", nested(detail, "team_b_id") or ""))
        for source in sources:
            for node in walk_dicts(source):
                coeff = next((numeric(node.get(k)) for k in coefficient_keys if numeric(node.get(k))), None)
                tid = str(node.get("team_id", node.get("competitor_id", "")))
                if coeff and tid and tid == team_a_id:
                    found["1"] = found["1"] or coeff
                elif coeff and tid and tid == team_b_id:
                    found["2"] = found["2"] or coeff
    return found["1"], found["2"]


def fail(msg, cls=web.HTTPBadRequest):
    raise cls(text=json.dumps({"error": msg}, ensure_ascii=False), content_type="application/json")


# ---------------- работа с API мини-аппа ----------------
async def throttle():
    """Скользящее окно: не больше RATE_MAX запросов к API за RATE_WINDOW секунд, чтобы не ловить 429."""
    async with _rate_lock:
        while True:
            now = time.monotonic()
            if now < _blocked_until:
                await asyncio.sleep(_blocked_until - now + 0.05)
                continue
            while _calls and now - _calls[0] >= RATE_WINDOW:
                _calls.popleft()
            if len(_calls) < RATE_MAX:
                _calls.append(now)
                return
            await asyncio.sleep(RATE_WINDOW - (now - _calls[0]) + 0.05)


def seconds_until(iso_time):
    if not iso_time:
        return None
    try:
        target = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0, int((target - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError):
        return None


async def raw_request(method, path, token=None, payload=None):
    await throttle()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return await _raw_request(method, path, headers, payload)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise RuntimeError(f"Нет связи с API CS3BET ({type(e).__name__}) — попробуй ещё раз") from None


async def _raw_request(method, path, headers, payload):
    async with get_session().request(method, f"{API_BASE}{path}", headers=headers, json=payload) as r:
        raw = await r.text()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw_response": raw[:500]}
        if r.status == 429:                      # сервер просит притормозить — короткая пауза для всех запросов
            global _blocked_until
            try:
                ra = int(float(r.headers.get("Retry-After", 0)))
            except ValueError:
                ra = 0
            _blocked_until = max(_blocked_until, time.monotonic() + (min(ra, 60) if ra > 0 else PAUSE_429))
            if isinstance(body, dict):
                body["_retry_after"] = ra
        return r.status, body


async def login(name, client, force=False):
    async with _lock(_login_locks, name):
        cached = _tokens.get(name)
        if cached and not force and time.time() - cached[1] < TOKEN_TTL:
            return cached[0]
        from telethon.tl.functions.messages import RequestMainWebViewRequest
        from telethon.errors import FloodWaitError
        from presence import ensure_connected
        webview = None
        for attempt in (1, 2):
            try:
                await ensure_connected(client)          # клиент мог отвалиться — переподключаем
                async with _tg_sem:
                    bot = await client.get_entity(BOT_USERNAME)
                    webview = await client(RequestMainWebViewRequest(peer=bot, bot=bot, platform="web"))
                break
            except FloodWaitError as e:
                raise RateLimited(e.seconds, flood=True)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                if attempt == 2:
                    raise RuntimeError(f"Нет связи с Telegram ({type(e).__name__}) — попробуй ещё раз") from None
                try:                                    # «Cannot send requests while disconnected»: сбрасываем и подключаемся заново
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(1)
        init_data = parse_qs(urlparse(webview.url).fragment, keep_blank_values=True).get("tgWebAppData", [None])[0]
        if not init_data:
            raise RuntimeError("Не удалось получить tgWebAppData")
        status, body = await raw_request("POST", "/api/auth/login", payload={"init_data": init_data})
        token = nested(body, "data", "access_token")
        if status == 429:
            raise RateLimited(body.get("_retry_after", 0))
        if status != 200 or not body.get("success") or not token:
            raise RuntimeError(f"Ошибка авторизации: HTTP {status}")
        _tokens[name] = (token, time.time())
        return token


async def call(name, client, method, path, payload=None):
    """Запрос с токеном аккаунта; при 401 перелогиниваемся один раз."""
    token = await login(name, client)
    status, body = await raw_request(method, path, token, payload)
    if status == 401:
        token = await login(name, client, force=True)
        status, body = await raw_request(method, path, token, payload)
    return status, body


@contextlib.asynccontextmanager
async def priority():
    """Пользовательское действие: пока оно идёт (и ещё пару секунд после), фоновый обход стоит."""
    global _prio, _prio_until
    _prio += 1
    try:
        yield
    finally:
        _prio -= 1
        _prio_until = time.monotonic() + 2.0


async def idle_gate():
    """Фоновый обход ждёт, пока пользователь не закончит свои дела, и оставляет ему запас запросов."""
    while True:
        now = time.monotonic()
        while _calls and now - _calls[0] >= RATE_WINDOW:     # чистим устаревшие записи окна (иначе ждали бы вечно)
            _calls.popleft()
        if _prio > 0 or now < _prio_until:
            await asyncio.sleep(0.3)
        elif now >= _blocked_until and len(_calls) >= RATE_MAX - USER_RESERVE:
            await asyncio.sleep(1)
        else:
            return


# ---------------- награды (общие функции) ----------------
async def read_rewards(name, client):
    ds, daily = await call(name, client, "GET", "/api/rewards/daily")
    if ds == 429:
        raise RateLimited(daily.get("_retry_after", 0))
    ws, wheel = await call(name, client, "GET", "/api/wheel/state")
    if ws == 429:
        raise RateLimited(wheel.get("_retry_after", 0))
    dd = nested(daily, "data") if ds == 200 else None
    wd = nested(wheel, "data") if ws == 200 else None
    dd = dd if isinstance(dd, dict) else {}
    wd = wd if isinstance(wd, dict) else {}
    d_claimable = dd.get("claimable") is True
    w_seconds = wd.get("remaining_seconds")
    try:
        w_seconds = max(0, int(float(w_seconds))) if w_seconds is not None else None
    except (TypeError, ValueError):
        w_seconds = None
    return {
        "daily": {"ok": ds == 200 and bool(dd), "claimable": d_claimable,
                  "seconds": 0 if d_claimable else seconds_until(dd.get("next_claim_at"))},
        "wheel": {"ok": ws == 200 and bool(wd), "available": wd.get("available") is True, "seconds": w_seconds},
    }


def summarize(status, r):
    ok = 200 <= status < 300 and r.get("success") is not False
    msg = r.get("message") or r.get("error") or r.get("detail") or ""
    msg = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
    out = {"ok": ok, "status": status, "message": msg[:200]}
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    if ok:
        # новый баланс и размер выигрыша — отдельными полями, а не сырым JSON в тексте
        for key in ("new_balance", "amount"):
            val = data.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                out[key] = val
    return out


# ---------------- фоновый обход аккаунтов ----------------
STATE: dict[str, dict] = {}     # аккаунт -> {balance, bets, rw, msg, error, ts}
_state_loaded = False
_cycle = {"running": False, "current": None, "index": 0, "total": 0, "round": 0}


def state_load():
    global STATE, _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        STATE = {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        STATE = {}


def state_save():
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(STATE, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[CS3BET] не удалось сохранить {STATE_FILE.name}: {e}", flush=True)


def set_balance(name, balance):
    """Пользовательские запросы (баланс после ставки и т.п.) тоже кладём в общий файл."""
    if balance is None:
        return
    rec = STATE.setdefault(name, {})
    rec["balance"] = balance
    rec["balance_ts"] = time.time()
    rec.pop("error", None)
    state_save()


class _Skip(Exception):
    """Аккаунт в этом круге пропускаем (причина — в тексте)."""


async def patient(fn):
    """Фоновый запрос: при 429 ждём 10 сек и повторяем, ещё 429 — ещё 10 сек, и так пока не пройдёт.
    Перед каждой попыткой уступаем дорогу пользователю."""
    for _ in range(MAX_429_TRIES):
        await idle_gate()
        try:
            status, body = await fn()
        except RateLimited as e:
            if e.flood and e.retry_after > 60:      # длинный FloodWait от Telegram — ждать нет смысла
                raise _Skip(f"FloodWait Telegram {e.retry_after} сек")
            await asyncio.sleep(max(PAUSE_429, e.retry_after) if e.flood else PAUSE_429)
            continue
        if status == 429:
            await asyncio.sleep(PAUSE_429)
            continue
        return status, body
    raise _Skip("лимит запросов (429), пропускаю до следующего круга")


def rw_needs_read(rec, now):
    rw = rec.get("rw")
    if not rw or now - rec.get("rw_ts", 0) > 3600:
        return True
    for key in ("daily", "wheel"):
        x = rw.get(key) or {}
        if not x.get("ok"):
            return True          # в прошлый раз не вышло прочитать — пробуем снова
        if x.get("due") is not None and now >= x["due"]:
            return True
    return False


def rw_pack(raw, now):
    """Таймеры в абсолютное время (unix сек), чтобы страница могла считать их сама."""
    out = {}
    for key, ready in (("daily", raw["daily"].get("claimable")), ("wheel", raw["wheel"].get("available"))):
        x = raw[key]
        sec = 0 if ready else x.get("seconds")
        out[key] = {"ok": bool(x.get("ok")), "due": (now + sec) if x.get("ok") and sec is not None else None}
    return out


async def refresh_account(name, client):
    """Один аккаунт: баланс, ставки, ежедневная, колесо. Всё успешное сразу пишется в файл."""
    rec = STATE.setdefault(name, {})
    now = time.time()
    try:
        # 1. баланс
        st, body = await patient(lambda: call(name, client, "GET", "/api/profile"))
        user = nested(body, "data", "user") or {}
        if st == 200 and body.get("success") and user.get("balance") is not None:
            rec["balance"], rec["balance_ts"] = user["balance"], time.time()
        else:
            raise RuntimeError(f"баланс: HTTP {st}")
        # 2. активные ставки
        st, body = await patient(lambda: call(name, client, "GET", "/api/bets/express/my?limit=100&offset=0"))
        if st == 200 and body.get("success") and isinstance(body.get("data"), list):
            active = [b for b in body["data"] if isinstance(b, dict) and b.get("status") == "pending"]
            pot = sum((Decimal(str(b.get("potential_payout") or 0)) for b in active), Decimal("0"))
            rec["bets"] = {"count": len(active), "potential": float(pot)}
        else:
            rec["bets"] = None
        # 3. ежедневная награда и колесо (читаем, только если пора; созревшее забираем)
        now = time.time()
        if rw_needs_read(rec, now):
            raw = await patient(lambda: _wrap(read_rewards(name, client)))
            raw = raw[1]
            rec["rw"], rec["rw_ts"] = rw_pack(raw, now), now
        rw = rec.get("rw") or {}
        want_d = bool((rw.get("daily") or {}).get("ok")) and (rw["daily"].get("due") or 1e18) <= now
        want_w = bool((rw.get("wheel") or {}).get("ok")) and (rw["wheel"].get("due") or 1e18) <= now
        if want_d or want_w:
            parts, ok_all, new_balance = [], True, None
            async with _lock(_rw_locks, name):
                for key, want, path, okt, label in (
                        ("daily", want_d, "/api/rewards/daily/claim", "Ежедневная награда получена", "Ежедневная награда"),
                        ("wheel", want_w, "/api/wheel/spin", "Колесо прокручено", "Колесо")):
                    if not want:
                        continue
                    st, r = await patient(lambda p=path: call(name, client, "POST", p))
                    res = summarize(st, r)
                    if res["ok"]:
                        parts.append(okt + (f" (+{res['amount']})" if res.get("amount") is not None else ""))
                        if res.get("new_balance") is not None:
                            new_balance = res["new_balance"]
                    else:
                        ok_all = False
                        parts.append(f"{label}: {res['message'] or 'не получилось'}")
                raw = (await patient(lambda: _wrap(read_rewards(name, client))))[1]
                rec["rw"], rec["rw_ts"] = rw_pack(raw, time.time()), time.time()
            if not ok_all:                      # не вышло забрать — не долбим каждый круг, повтор через минуту
                for key, want in (("daily", want_d), ("wheel", want_w)):
                    x = rec["rw"].get(key) or {}
                    if want and x.get("ok") and x.get("due") is not None and x["due"] <= time.time():
                        x["due"] = time.time() + 60
            rec["msg"], rec["msg_ok"], rec["msg_ts"] = " · ".join(parts), ok_all, time.time()
            if new_balance is not None:
                rec["balance"], rec["balance_ts"] = new_balance, time.time()
            else:
                st, body = await patient(lambda: call(name, client, "GET", "/api/profile"))
                bal = nested(body, "data", "user", "balance")
                if st == 200 and bal is not None:
                    rec["balance"], rec["balance_ts"] = bal, time.time()
        rec.pop("error", None)
    except _Skip as e:
        rec["error"], rec["error_ts"] = str(e)[:200], time.time()
    except RateLimited as e:
        rec["error"], rec["error_ts"] = f"лимит запросов ({e.retry_after} сек)", time.time()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        rec["error"], rec["error_ts"] = (str(e)[:200] or type(e).__name__), time.time()
    rec["ts"] = time.time()
    state_save()


async def _wrap(coro):
    """read_rewards возвращает словарь, а patient ждёт (status, body) — оборачиваем."""
    return 200, await coro


async def poller_loop(get_clients):
    state_load()
    while True:
        try:
            clients = get_clients()
            if not clients:
                await asyncio.sleep(5)
                continue
            names = list(clients)
            _cycle.update(running=True, total=len(names), round=_cycle["round"] + 1)
            for i, name in enumerate(names, 1):
                client = get_clients().get(name)
                if client is None:
                    continue
                _cycle.update(current=name, index=i)
                await refresh_account(name, client)
                await asyncio.sleep(0.5)
            _cycle.update(current=None)
            await asyncio.sleep(CYCLE_GAP)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[CS3BET] обход: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(10)


# ---------------- хендлеры ----------------
def make_handlers(get_clients, app=None, account_ids=None):
    def client_for(request):
        name = request.match_info["acc"]
        client = get_clients().get(name)
        if client is None:
            fail("Аккаунт не найден или не подключён", web.HTTPNotFound)
        return name, client

    async def balance(request):
        async with priority():
            return await balance_inner(request)

    async def balance_inner(request):
        name, client = client_for(request)
        try:
            status, body = await call(name, client, "GET", "/api/profile")
        except RateLimited as e:
            return web.json_response({"balance": None, "rate_limited": True, "retry_after": e.retry_after, "error": "лимит запросов"})
        except Exception as e:
            return web.json_response({"balance": None, "error": str(e)[:200]})
        if status == 429:
            return web.json_response({"balance": None, "rate_limited": True, "retry_after": body.get("_retry_after", 0), "error": "лимит запросов"})
        user = nested(body, "data", "user") or {}
        if status != 200 or not body.get("success") or user.get("balance") is None:
            return web.json_response({"balance": None, "error": f"HTTP {status}"})
        set_balance(name, user.get("balance"))
        return web.json_response({"balance": user.get("balance")})

    async def fetch_matches(name, client, selected_ids=None):
        status, resp = await call(name, client, "GET", "/api/matches/active")
        if status != 200 or not resp.get("success"):
            raise RuntimeError(f"Не удалось получить матчи: HTTP {status}")
        data = resp.get("data") or {}
        matches = data.get("matches", []) if isinstance(data, dict) else data if isinstance(data, list) else []

        async def one(m):
            mid = m.get("id", m.get("match_id"))
            if mid is None:
                return None
            d_status, d_resp = await call(name, client, "GET", f"/api/matches/{mid}/detail")
            detail = nested(d_resp, "data") if d_status == 200 and d_resp.get("success") else {}
            detail = detail if isinstance(detail, dict) else {}
            merged = {**m, **detail}
            odds_a, odds_b = extract_coefficients(m, detail)
            return {"id": int(mid),
                    "team_a": str(merged.get("team_a_name") or merged.get("team_a") or "Команда 1"),
                    "team_b": str(merged.get("team_b_name") or merged.get("team_b") or "Команда 2"),
                    "odds_a": float(odds_a) if odds_a else None,
                    "odds_b": float(odds_b) if odds_b else None,
                    "status": merged.get("status")}

        if selected_ids is not None:
            matches = [m for m in matches if m.get('id', m.get('match_id')) in selected_ids or str(m.get('id', m.get('match_id'))) in {str(i) for i in selected_ids}]
        res = await asyncio.gather(*(one(m) for m in matches), return_exceptions=True)
        items = [r for r in res if isinstance(r, dict)]
        if matches and not items:     # все запросы упали — это не «матчей нет», а ошибка (лимит, обрыв связи)
            err = next((r for r in res if isinstance(r, Exception)), None)
            if err:
                raise err
        return items

    async def express_stats(request):
        name, client = client_for(request)
        try:
            status, body = await call(name, client, "GET", "/api/bets/express/my?limit=100&offset=0")
        except RateLimited as e:
            return web.json_response({"count": None, "potential": None, "rate_limited": True,
                                     "retry_after": e.retry_after, "error": "лимит запросов"})
        except Exception as e:
            return web.json_response({"count": None, "potential": None, "error": str(e)[:200]})
        if status == 429:
            return web.json_response({"count": None, "potential": None, "rate_limited": True,
                                      "retry_after": body.get("_retry_after", 0), "error": "лимит запросов"})
        if status != 200 or not body.get("success") or not isinstance(body.get("data"), list):
            return web.json_response({"count": None, "potential": None, "error": f"HTTP {status}"})
        active = [bet for bet in body["data"] if isinstance(bet, dict) and bet.get("status") == "pending"]
        potential = sum((Decimal(str(bet.get("potential_payout") or 0)) for bet in active), Decimal("0"))
        return web.json_response({"count": len(active), "potential": float(potential)})

    async def matches(request):
        name, client = client_for(request)
        async with priority():          # открыл «ставки» — фоновый обход ждёт
            try:
                items = await fetch_matches(name, client)
            except Exception as e:
                fail(str(e)[:300])
        return web.json_response({"items": items})

    async def bet(request):
        name, client = client_for(request)
        data = await request.json()
        stake_raw = str(data.get("stake", "")).strip().lower()
        stake = None                       # "all" -> весь баланс, считаем внутри блокировки
        if stake_raw != "all":
            try:
                stake = Decimal(stake_raw.replace(",", "."))
            except (InvalidOperation, ValueError):
                fail("Неверная сумма ставки")
            if not stake.is_finite() or stake < MIN_STAKE:
                fail(f"Минимальная ставка — {MIN_STAKE} монет")
            if stake > MAX_STAKE:
                fail("Максимальная ставка — 50000 монет")
            if stake != stake.quantize(Decimal("0.01")):
                fail("Сумма — максимум 2 знака после запятой")
        raw_legs = data.get("legs") or []
        if not raw_legs:
            fail("Не выбрано ни одного исхода")
        seen, picks = set(), []
        for leg in raw_legs:
            try:
                mid, side = int(leg["match_id"]), str(leg["side"])
            except (KeyError, TypeError, ValueError):
                fail("Неверный формат выбора")
            if side not in ("1", "2"):
                fail("Исход должен быть 1 или 2")
            if mid in seen:
                fail("Один матч выбран дважды")
            seen.add(mid)
            picks.append((mid, side, numeric(leg.get("odds"))))

        state = {"sent": False}
        try:
            async with priority():      # ставка (в т.ч. по шаблону) — приоритет над фоновым обходом
                return await place_bet(name, client, stake, picks, state)
        except RateLimited as e:
            fail(f"Лимит запросов API, подожди {e.retry_after or 60} сек. и повтори. Ставка не отправлена.", web.HTTPTooManyRequests)
        except web.HTTPException:
            raise
        except Exception as e:
            why = str(e)[:250] or type(e).__name__
            if state["sent"]:   # обрыв уже во время отправки: сервер мог принять ставку
                fail(f"{why}. Ответ на ставку не получен — она могла пройти. Проверь баланс, прежде чем повторять.")
            fail(f"{why}. Ставка не отправлена.")

    async def place_bet(name, client, stake, picks, state):
        async with _lock(_bet_locks, name):   # защита от двойного клика
            p_status, p_body = await call(name, client, "GET", "/api/profile")
            bal = numeric(nested(p_body, "data", "user", "balance")) if p_status == 200 else None
            if bal is None:
                fail("Не удалось получить положительный актуальный баланс")
            if stake is None:
                stake = min(bal.quantize(Decimal("0.01"), rounding=ROUND_DOWN), MAX_STAKE)
            if not stake.is_finite() or stake < MIN_STAKE or stake > MAX_STAKE or stake > bal:
                fail("Недостаточно баланса или сумма вне лимита 100–50000 монет")
            # Свежие данные по выбранным матчам прямо перед ставкой
            current = {m["id"]: m for m in await fetch_matches(name, client, {mid for mid, _, _ in picks})}
            legs, combined = [], Decimal("1")
            for mid, side, seen_odds in picks:
                m = current.get(mid)
                if not m or m["status"] != "betting_open":
                    fail(f"Матч {mid} закрыт для ставок или исчез. Обнови список матчей.")
                now = m["odds_a"] if side == "1" else m["odds_b"]
                if now is None:
                    fail(f"Не удалось проверить коэффициент в матче {m['team_a']} — {m['team_b']}")
                now = Decimal(str(now))
                if seen_odds is not None and abs(now - seen_odds) >= Decimal("0.005"):
                    fail(f"Коэффициент изменился: {m['team_a']} — {m['team_b']} (было {seen_odds}, стало {now}). Обнови список.")
                combined *= now
                legs.append({"match_id": mid, "type_id": 1, "specifiers": {}, "outcome_external_id": side})

            if combined > MAX_ODDS:
                fail("Общий коэффициент превышает максимум 30")
            await login(name, client)        # токен/связь с Telegram — до отправки, ставка ещё не ушла
            if state.get("on_send"):
                state["on_send"]()
            state["sent"] = True
            if len(legs) == 1:
                kind = "single"
                payload = {"stake": float(stake), **legs[0]}
                # Путь одиночной ставки неизвестен: перебираем вероятные. 404/405 = пути нет (ставка не создана).
                tried = []
                for path in SINGLE_CANDIDATES:
                    status, result = await call(name, client, "POST", path, payload)
                    tried.append(f"{path}:{status}")
                    if status not in (404, 405):
                        break
                else:
                    result = {"message": "ни один путь не найден: " + ", ".join(tried)}
            else:
                kind, path = "express", BET_EXPRESS_PATH
                payload = {"stake": float(stake), "legs": legs}
                status, result = await call(name, client, "POST", path, payload)
            ok = 200 <= status < 300 and bool(result.get("success"))
            if ok:
                set_balance(name, float(bal-stake))
            return web.json_response({
                "ok": ok, "kind": kind, "status": status, "stake": float(stake), "coefficient": float(round(combined, 4)),
                "potential": float(round(stake * combined, 2)), "response": result,
                "error": None if ok else f"HTTP {status} {path}: " + str(result.get("message") or result.get("error") or result.get("detail") or result)[:300],
            })

    async def template_get(request):
        try:
            legs = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8")).get("legs", [])
        except (OSError, ValueError):
            legs = []
        return web.json_response({"legs": legs})

    async def template_set(request):
        data = await request.json()
        legs, seen = [], set()
        for l in data.get("legs") or []:
            try:
                mid, side = int(l["match_id"]), str(l["side"])
            except (KeyError, TypeError, ValueError):
                fail("Неверный формат шаблона")
            if side not in ("1", "2") or mid in seen:
                fail("Неверный исход или матч выбран дважды")
            seen.add(mid)
            legs.append({"match_id": mid, "side": side,
                         "team_a": str(l.get("team_a", ""))[:80], "team_b": str(l.get("team_b", ""))[:80]})
        TEMPLATE_FILE.write_text(json.dumps({"legs": legs}, ensure_ascii=False), encoding="utf-8")
        return web.json_response({"legs": legs})

    async def promo(request):
        name, client = client_for(request)
        data = await request.json()
        code = str(data.get("code", "")).strip()
        if not code or len(code) > 64:
            fail("Введи промокод (до 64 символов)")
        try:
            status, result = await call(name, client, "POST", "/api/promocodes/redeem", {"code": code})
        except RateLimited as e:
            return web.json_response({"ok": False, "rate_limited": True, "retry_after": e.retry_after, "message": "лимит запросов"})
        except Exception as e:
            return web.json_response({"ok": False, "message": str(e)[:200]})
        if status == 429:
            return web.json_response({"ok": False, "rate_limited": True, "retry_after": result.get("_retry_after", 0), "message": "лимит запросов"})
        msg = result.get("message") or result.get("error") or result.get("detail") or nested(result, "data", "message")
        if not isinstance(msg, str):
            msg = json.dumps(msg if msg is not None else result.get("data", result), ensure_ascii=False)
        ok = 200 <= status < 300 and result.get("success") is not False
        return web.json_response({"ok": ok, "status": status, "message": msg[:300]})

    async def rewards_state(request):
        name, client = client_for(request)
        try:
            state = await read_rewards(name, client)
        except RateLimited as e:
            return web.json_response({"rate_limited": True, "retry_after": e.retry_after})
        except Exception as e:
            return web.json_response({"error": str(e)[:200]})
        return web.json_response({"state": state})

    async def rewards_collect(request):
        """Забрать то, что созрело (daily и/или wheel), и сразу вернуть новое состояние таймеров."""
        name, client = client_for(request)
        data = await request.json()
        results = {}
        async with _lock(_rw_locks, name):
            try:
                if data.get("daily"):
                    st, r = await call(name, client, "POST", "/api/rewards/daily/claim")
                    if st == 429:
                        raise RateLimited(r.get("_retry_after", 0))
                    results["daily"] = summarize(st, r)
                if data.get("wheel"):
                    st, r = await call(name, client, "POST", "/api/wheel/spin")
                    if st == 429:
                        raise RateLimited(r.get("_retry_after", 0))
                    results["wheel"] = summarize(st, r)
                state = await read_rewards(name, client)
            except RateLimited as e:
                return web.json_response({"rate_limited": True, "retry_after": e.retry_after, "results": results})
            except Exception as e:
                return web.json_response({"error": str(e)[:200], "results": results})
        return web.json_response({"results": results, "state": state})

    async def state_get(request):
        """Данные для списка аккаунтов — из файла, без запросов к API."""
        items = {}
        for name in get_clients():
            rec = STATE.get(name) or {}
            items[name] = {k: rec.get(k) for k in ("balance", "bets", "rw", "msg", "msg_ok", "msg_ts", "error", "ts")}
        return web.json_response({"now": time.time(), "items": items,
                                  "cycle": {"current": _cycle["current"], "index": _cycle["index"],
                                            "total": _cycle["total"], "round": _cycle["round"]}})

    if app is not None:
        async def bulk_fetch(name, client):
            async with priority():
                return await fetch_matches(name, client)
        async def bulk_submit(name, client, stake, picks, state):
            async with priority():
                return await place_bet(name, client, stake, picks, state)
        bulk = BulkGame(Path(__file__).resolve().parent, get_clients, STATE, bulk_fetch, bulk_submit, account_ids)
        bulk.register(app)
        app['cs3bet_bulk'] = bulk

    return (balance, express_stats, matches, bet, template_get, template_set, promo, rewards_state,
            rewards_collect, state_get)


def register_all(app, get_clients, account_ids=None):
    state_load()
    (balance, express_stats, matches, bet, template_get, template_set, promo, rewards_state,
     rewards_collect, state_get) = make_handlers(get_clients, app, account_ids)

    async def start_poller(app):
        app["cs3bet_poller"] = asyncio.create_task(poller_loop(get_clients))

    async def stop_poller(app):
        task = app.get("cs3bet_poller")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app.on_startup.append(start_poller)
    app.on_cleanup.append(stop_poller)
    state_load()
    app.add_routes([
        web.get("/api/cs3bet/state", state_get),
        web.get("/api/cs3bet/balance/{acc}", balance),
        web.get("/api/cs3bet/express-stats/{acc}", express_stats),
        web.get("/api/cs3bet/matches/{acc}", matches),
        web.post("/api/cs3bet/bet/{acc}", bet),
        web.get("/api/cs3bet/template", template_get),
        web.post("/api/cs3bet/template", template_set),
        web.post("/api/cs3bet/promo/{acc}", promo),
        web.get("/api/cs3bet/rewards/{acc}", rewards_state),
        web.post("/api/cs3bet/rewards/{acc}/collect", rewards_collect),
    ])