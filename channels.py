"""
Модуль «Каналы»: подписка аккаунтов на канал с задержками.

Подключение в main.py (aiohttp):

    import channels
    channels.register(app, lambda: {n: a.client for n, a in ACCOUNTS.items() if a.me is not None})

Эндпоинты:
    POST /api/channels/plan   {link,count,delay_from,delay_to} -> план (ничего не запускает)
    POST /api/channels/start  {plan_id}                        -> запускает подтверждённый план
    POST /api/channels/cancel {plan_id}                        -> отменяет неподтверждённый план (освобождает аккаунты)
    POST /api/channels/stop   {job_id?}                        -> останавливает задачу (без job_id — все активные)
    GET  /api/channels/job                                     -> {"jobs": [...]} — все задачи (можно несколько одновременно)
"""
import asyncio
import itertools
import random
import re
import time
import uuid


def parse_link(link):
    """-> ('invite', hash) | ('public', username)"""
    s = (link or "").strip()
    if not s:
        raise ValueError("Укажи ссылку на канал")
    m = re.search(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", s, re.I)
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z][\w]{3,})", s, re.I)
    if m:
        return "public", m.group(1)
    m = re.fullmatch(r"@?([A-Za-z][\w]{3,})", s)
    if m:
        return "public", m.group(1)
    raise ValueError("Не похоже на ссылку на канал (нужна t.me/... или @username)")


_SEQ = itertools.count(1001)   # сквозной номер заказа (#1001, #1002, ...)

_USERNAMES = lambda: {}


def set_usernames(fn):
    """main.py передаёт функцию -> {имя аккаунта: username}. Нужна, чтобы показывать @username в планах."""
    global _USERNAMES
    _USERNAMES = fn


def usernames():
    try:
        return _USERNAMES() or {}
    except Exception:
        return {}


class ChannelJoiner:
    service = ""   # имя услуги (= префикс роутов), ставится в register()
    parse = staticmethod(parse_link)  # переопределяется в posts.py (ссылка на пост)
    ALREADY_ALL = "Все {n} подключённых аккаунтов уже подписаны"

    def __init__(self, get_clients):
        self.get_clients = get_clients
        self.plans = {}
        self.jobs = {}  # job_id -> job; несколько задач работают параллельно

    # ---------- план ----------
    def _alive(self):
        return {n: c for n, c in self.get_clients().items() if c and c.is_connected()}

    async def make_plan(self, link, count, dmin, dmax, extra=None):
        kind, key = self.parse(link)
        try:
            count, dmin, dmax = int(count), int(dmin), int(dmax)
        except (TypeError, ValueError):
            raise ValueError("Количество и задержки должны быть числами")
        if count < 1:
            raise ValueError("Количество аккаунтов должно быть не меньше 1")
        if dmin < 0 or dmax < 0:
            raise ValueError("Задержка не может быть отрицательной")
        if dmax < dmin:
            raise ValueError("«Задержка до» не может быть меньше «Задержка от»")
        for k in [k for k, v in self.plans.items() if time.time() - v["created"] > 600]:
            self.plans.pop(k, None)
        alive = self._alive()
        if not alive:
            raise ValueError("Нет подключённых аккаунтов")

        title = await self._title(next(iter(alive.values())), kind, key)

        # смотрим, кто уже подписан: такие аккаунты в план не попадают, распределяем только остальных
        subscribed, check_errors = await self._subscribed(alive, kind, key)
        free, already = self._candidates(alive, subscribed)
        requested = count
        count = min(count, len(free))   # просят больше, чем есть свободных — берём всех свободных

        names = random.sample(free, count)
        items = []
        at = 0
        for n in names:
            d = random.randint(dmin, dmax)
            at += d
            items.append({"account": n, "delay": d, "at": at})
        pid = uuid.uuid4().hex
        plan = {"id": pid, "link": link.strip(), "kind": kind, "key": key, "title": title,
                "items": items, "total": at, "created": time.time(), "extra": extra,
                "already": already, "alive": len(alive), "requested": requested, "check_errors": check_errors}
        self.plans[pid] = plan
        return self._public_plan(plan)

    def _candidates(self, alive, subscribed):
        """-> (кого берём в план, кто исключён). Для подписки: берём тех, кто ещё не подписан."""
        free = sorted(n for n in alive if n not in subscribed)
        if subscribed and not free:
            raise ValueError(self.ALREADY_ALL.format(n=len(alive)))
        return free, sorted(subscribed)

    async def _title(self, client, kind, key):
        """Название канала для плана. Явные ошибки ссылки — пробрасываем, остальное игнорируем."""
        from telethon.tl.functions.messages import CheckChatInviteRequest
        try:
            if kind == "invite":
                r = await client(CheckChatInviteRequest(key))
                return getattr(r, "title", None) or getattr(getattr(r, "chat", None), "title", None)
            ent = await client.get_entity(key)
            return getattr(ent, "title", None) or key
        except Exception as e:
            n = type(e).__name__
            if n in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                raise ValueError("Канал с таким username не найден")
            if n in ("InviteHashInvalidError", "InviteHashExpiredError"):
                raise ValueError("Инвайт-ссылка недействительна или истекла")
            return None

    async def _subscribed(self, alive, kind, key):
        """-> (имена аккаунтов, уже подписанных на канал; сколько аккаунтов не удалось проверить).
        Не удалось проверить — аккаунт остаётся в плане (при входе «уже в канале» всё равно обработается)."""
        from telethon.tl import types
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.functions.messages import CheckChatInviteRequest
        sem = asyncio.Semaphore(8)

        async def one(client):
            async with sem:
                try:
                    if kind == "invite":
                        # аккаунт уже в чате по этой инвайт-ссылке -> ChatInviteAlready
                        r = await client(CheckChatInviteRequest(key))
                        if not isinstance(r, types.ChatInviteAlready):
                            return False
                        # Telegram отдаёт «Already» и для тех, кто вышел/был удалён — смотрим флаг left
                        return not getattr(getattr(r, "chat", None), "left", False)
                    peer = await client.get_input_entity(key)
                    r = await client(GetParticipantRequest(peer, types.InputPeerSelf()))
                    return not isinstance(r.participant, (types.ChannelParticipantBanned, types.ChannelParticipantLeft))
                except Exception as e:
                    n = type(e).__name__
                    if n in ("UserNotParticipantError", "ChannelPrivateError"):
                        return False
                    print(f"[CHANNELS] не удалось проверить подписку: {n}: {e}"[:200], flush=True)
                    return None  # не смогли проверить

        names = sorted(alive)
        res = await asyncio.gather(*(one(alive[n]) for n in names))
        print(f"[CHANNELS] проверка подписки ({kind}): подписаны {sum(r is True for r in res)}, "
              f"не подписаны {sum(r is False for r in res)}, не удалось проверить {sum(r is None for r in res)}", flush=True)
        return {n for n, r in zip(names, res) if r is True}, sum(1 for r in res if r is None)

    @staticmethod
    def _public_plan(p):
        out = {k: p[k] for k in ("id", "link", "title", "total")}
        un = usernames()
        out["items"] = [{**i, "username": un.get(i["account"], "")} for i in p["items"]]
        out.update(already=p.get("already", []), alive=p.get("alive", 0), requested=p.get("requested"), check_errors=p.get("check_errors", 0))
        return out

    # ---------- запуск ----------
    async def start(self, plan_id):
        plan = self.plans.pop(plan_id, None)
        if not plan:
            raise ValueError("План устарел — построй его заново")
        # храним максимум 10 завершённых задач
        done = [k for k, v in self.jobs.items() if v["status"] != "running"]
        for k in done[:-9] if len(done) > 9 else []:
            self.jobs.pop(k, None)
        job = {
            "id": plan["id"], "link": plan["link"], "title": plan["title"],
            "status": "running", "total": plan["total"], "extra": plan.get("extra"), "started": time.time(),
            "items": [{"account": i["account"], "delay": i["delay"], "at": i["at"],
                       "state": "queue", "note": "", "until": None} for i in plan["items"]],
            "stop": asyncio.Event(), "skip": asyncio.Event(),
            "seq": next(_SEQ), "service": self.service,
        }
        self.jobs[job["id"]] = job
        job["task"] = asyncio.ensure_future(self._run(job, plan["kind"], plan["key"]))
        return {"job": self._pub(job)}

    async def _run(self, job, kind, key):
        try:
            for it in job["items"]:
                it["state"], it["until"] = "wait", time.time() + it["delay"]
                job["skip"].clear()
                if await self._sleep(job, it["delay"]):
                    break  # нажали «Остановить»
                it["state"], it["until"] = "active", None
                client = self.get_clients().get(it["account"])
                if not client or not client.is_connected():
                    it["state"], it["note"], it["finished"] = "err", "аккаунт не подключён", time.time()
                    continue
                it["state"], it["note"] = await self._join(client, kind, key, job.get("extra"))
                it["finished"] = time.time()
            job["status"] = "stopped" if job["stop"].is_set() else "done"
        except Exception as e:  # не даём задаче зависнуть в «running»
            job["status"], job["error"] = "done", str(e)
        finally:
            for it in job["items"]:
                if it["state"] in ("queue", "wait"):
                    it["state"], it["until"] = "skip", None

    @staticmethod
    async def _sleep(job, delay):
        """Ждёт delay секунд. Прерывается на «Остановить» (-> True) и на «Ускорить» (ожидание текущего аккаунта = 0 -> False)."""
        waiters = {asyncio.ensure_future(job["stop"].wait()), asyncio.ensure_future(job["skip"].wait())}
        try:
            await asyncio.wait(waiters, timeout=max(0, delay), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
        job["skip"].clear()
        return job["stop"].is_set()

    def skip(self, job_id=None):
        """«Ускорить»: у аккаунта, который сейчас ждёт своей очереди, таймер обнуляется — он работает сразу."""
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        if job_id:
            running = [j for j in running if j["id"] == job_id]
        if not running:
            raise ValueError("Заказ уже не выполняется")
        for j in running:
            if any(i["state"] == "wait" for i in j["items"]):
                j["skip"].set()
        return {"ok": True}

    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest
        try:
            if kind == "invite":
                await client(ImportChatInviteRequest(key))
            else:
                await client(JoinChannelRequest(await client.get_entity(key)))
            return "ok", "вошёл"
        except Exception as e:
            n = type(e).__name__
            if n == "UserAlreadyParticipantError":
                return "ok", "уже в канале"
            if n == "InviteRequestSentError":
                return "ok", "заявка на вступление отправлена"
            if n == "FloodWaitError":
                return "err", f"FloodWait: подождать {getattr(e, 'seconds', '?')} сек"
            return "err", f"{n}: {e}"[:200]

    def cancel(self, plan_id):
        self.plans.pop(plan_id, None)
        return {"ok": True}

    def exclude(self, plan_id, account):
        """Убирает аккаунт из построенного плана; задержки остальных сохраняются, общее время пересчитывается."""
        plan = self.plans.get(plan_id)
        if not plan:
            raise ValueError("План устарел — построй его заново")
        items = [i for i in plan["items"] if i["account"] != str(account)]
        if len(items) == len(plan["items"]):
            raise ValueError("Этого аккаунта нет в плане")
        if not items:
            raise ValueError("В плане должен остаться хотя бы один аккаунт — иначе нажми «Отмена»")
        at = 0
        for i in items:
            at += i["delay"]
            i["at"] = at
        plan["items"], plan["total"] = items, at
        return self._public_plan(plan)

    def stop(self, job_id=None):
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        if job_id:
            running = [j for j in running if j["id"] == job_id]
        if not running:
            raise ValueError("Нет активной подписки")
        for j in running:
            j["stop"].set()
        return {"ok": True}

    @staticmethod
    def _pub(j):
        now = time.time()
        un = usernames()
        items = [{"account": i["account"], "username": un.get(i["account"], ""), "delay": i["delay"], "at": i["at"], "state": i["state"],
                  "note": i["note"], "wait_left": max(0, round(i["until"] - now)) if i["until"] else 0,
                  "finished": i.get("finished")}
                 for i in j["items"]]
        return {"id": j["id"], "link": j["link"], "title": j["title"], "status": j["status"],
                "total": j["total"], "items": items, "error": j.get("error"),
                "seq": j.get("seq"), "service": j.get("service", ""), "started": j.get("started"), "now": now}

    def public_jobs(self):
        return {"jobs": [self._pub(j) for j in self.jobs.values()]}


def register(app, get_clients, prefix="channels", joiner_cls=None):
    """Добавляет роуты /api/channels/* в aiohttp-приложение (авторизация — ваша auth_mw)."""
    from aiohttp import web

    joiner = (joiner_cls or ChannelJoiner)(get_clients)
    joiner.service = prefix

    async def body(request):
        try:
            return await request.json()
        except Exception:
            return {}

    async def run(fn):
        try:
            return web.json_response(await fn())
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def plan(request):
        d = await body(request)
        return await run(lambda: joiner.make_plan(d.get("link"), d.get("count"),
                                                  d.get("delay_from"), d.get("delay_to"), d.get("extra")))

    async def start(request):
        d = await body(request)
        return await run(lambda: joiner.start(d.get("plan_id")))

    async def stop(request):
        d = await body(request)

        async def f():
            return joiner.stop(d.get("job_id"))
        return await run(f)

    async def skip(request):
        d = await body(request)

        async def f():
            return joiner.skip(d.get("job_id"))
        return await run(f)

    async def cancel(request):
        d = await body(request)

        async def f():
            return joiner.cancel(d.get("plan_id"))
        return await run(f)

    async def exclude(request):
        d = await body(request)

        async def f():
            return joiner.exclude(d.get("plan_id"), d.get("account"))
        return await run(f)

    async def job(request):
        return web.json_response(joiner.public_jobs())

    app.add_routes([
        web.post(f"/api/{prefix}/plan", plan),
        web.post(f"/api/{prefix}/start", start),
        web.post(f"/api/{prefix}/stop", stop),
        web.post(f"/api/{prefix}/cancel", cancel),
        web.post(f"/api/{prefix}/exclude", exclude),
        web.post(f"/api/{prefix}/skip", skip),
        web.get(f"/api/{prefix}/job", job),
    ])
    if joiner_cls is None:  # заодно подключаем вкладки «Реакции» и «Просмотры» (posts.py): main.py менять не нужно
        try:
            import posts
        except ImportError:
            posts = None
        if posts:
            posts.register_all(app, get_clients)
    return joiner