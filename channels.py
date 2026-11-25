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
import json
from pathlib import Path
import random
import re
import time
import uuid

from aiohttp import web

import plan_options
from template_library import atomic_json_save


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


_NEXT_SEQ = 1001
ORDER_JOBS_DIRECTORY = web.AppKey("order_jobs_directory", Path)
ORDER_ACCOUNT_CONNECTING = web.AppKey("order_account_connecting")
ORDER_ACCOUNT_IDS = web.AppKey("order_account_ids")
ORDER_ACCOUNT_ALLOWED = web.AppKey("order_account_allowed")
RUNTIME_JOB_FIELDS = {"task", "stop", "skip", "resumed"}

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
        self.storage = None
        self.account_connecting = lambda name: False
        self.account_allowed = None
        self.account_ids = lambda: {}

    def save_jobs(self):
        if self.storage is not None:
            jobs = [{key: value for key, value in job.items() if key not in RUNTIME_JOB_FIELDS}
                    for job in self.jobs.values()]
            self.storage.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_json_save(self.storage, jobs)

    def load_jobs(self, storage):
        global _NEXT_SEQ
        self.storage = storage
        try:
            records = json.loads(storage.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        for job in records:
            _NEXT_SEQ = max(_NEXT_SEQ, job["seq"] + 1)
            for item in job["items"]:
                if item["state"] == "active":
                    item.update(state="err", note="Исход действия неизвестен после перезапуска", until=None)
                elif job.get("stop_requested") and item["state"] in ("wait", "queue"):
                    item.update(state="skip", until=None)
            if job.get("stop_requested"):
                job["status"] = "stopped"
            self.jobs[job["id"]] = job
        self.save_jobs()

    async def resume_jobs(self, app=None):
        for job in self.jobs.values():
            if job["status"] == "running":
                job.update(stop=asyncio.Event(), skip=asyncio.Event(), resumed=True)
                job["task"] = asyncio.create_task(self._run(job, job["kind"], job["key"]))

    async def suspend_jobs(self, app=None):
        tasks = [job["task"] for job in self.jobs.values() if job.get("task") and not job["task"].done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- план ----------
    def _alive(self):
        return {n: c for n, c in self.get_clients().items() if c and c.is_connected()}

    async def eligible(self, extra):
        return await plan_options.eligible(self._alive(), extra)

    def store_plan(self, plan, extra):
        if not plan["items"]:
            raise ValueError("Нет подходящих аккаунтов")
        finish = plan_options.deadline((extra or {}).get("finish_at"))
        quiet = plan_options.quiet_hours(extra)
        for item in plan["items"]:
            item["base_delay"] = item["delay"]
        plan.update(finish_at=finish, quiet=quiet)
        plan["total"] = plan_options.schedule_order(plan["items"], time.time(), finish, quiet)
        self.plans[plan["id"]] = plan
        return self._public_plan(plan)

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
        alive = await self.eligible(extra)
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
        return self.store_plan(plan, extra)

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
        out["items"] = [{k: i[k] for k in ("account", "delay", "at")} |
                        {"username": un.get(i["account"], "")} for i in p["items"]]
        out["finish_at"] = p.get("finish_at")
        out.update(already=p.get("already", []), alive=p.get("alive", 0), requested=p.get("requested"), check_errors=p.get("check_errors", 0))
        return out

    # ---------- запуск ----------
    async def start(self, plan_id):
        global _NEXT_SEQ
        plan = self.plans.get(plan_id)
        if not plan:
            raise ValueError("План устарел — построй его заново")
        if self.account_allowed and any(not self.account_allowed(item["account"]) for item in plan["items"]):
            raise ValueError("Метки аккаунтов изменились. Построй план заново.")
        finish = plan_options.deadline(plan["finish_at"])
        started = time.time()
        plan["total"] = plan_options.schedule_order(plan["items"], started, finish, plan["quiet"])
        previous_jobs = self.jobs.copy()
        ids = self.account_ids()
        # храним максимум 10 завершённых задач
        done = [k for k, v in self.jobs.items() if v["status"] != "running"]
        for k in done[:-9] if len(done) > 9 else []:
            self.jobs.pop(k, None)
        job = {
            "id": plan["id"], "link": plan["link"], "title": plan["title"],
            "status": "running", "total": plan["total"], "finish_at": plan.get("finish_at"), "extra": plan.get("extra"), "started": started, "quiet": plan["quiet"],
            "items": [{"account": i["account"], "delay": i["delay"], "at": i["at"], "base_delay": i["base_delay"],
                       "telegram_id": ids.get(i["account"]),
                       "state": "queue", "note": "", "until": None} for i in plan["items"]],
            "stop": asyncio.Event(), "skip": asyncio.Event(),
            "seq": _NEXT_SEQ, "service": self.service, "kind": plan["kind"], "key": plan["key"],
        }
        self.jobs[job["id"]] = job
        try:
            self.save_jobs()
        except OSError:
            self.jobs = previous_jobs
            raise
        _NEXT_SEQ += 1
        self.plans.pop(plan_id)
        job["task"] = asyncio.ensure_future(self._run(job, plan["kind"], plan["key"]))
        return {"job": self._pub(job)}

    async def _run(self, job, kind, key):
        previous = job.get("started")
        try:
            for item in job["items"]:
                if item["state"] not in ("queue", "wait"):
                    previous = item.get("finished") or previous
                    continue
                if await self._wait_for_account(job, item, previous):
                    break
                client = await self._ready_client(job, item)
                if job["stop"].is_set():
                    break
                if not client:
                    item.update(state="err", note=item["note"] or "аккаунт не подключён", until=None)
                else:
                    item.update(state="active", until=None, note="")
                    self.save_jobs()
                    item["state"], item["note"] = await self._perform(job, item, client, kind, key)
                previous = item["finished"] = time.time()
                self.save_jobs()
            job["status"] = "stopped" if job["stop"].is_set() else "done"
        except asyncio.CancelledError:
            # The persisted checkpoint remains available for the next instance.
            raise
        except Exception as exc:
            job["status"], job["error"] = "done", str(exc)
        for item in job["items"]:
            if item["state"] in ("queue", "wait"):
                item.update(state="skip", until=None)
            elif item["state"] == "active":
                item.update(state="err", note="Исход действия неизвестен", until=None)
        self.save_jobs()

    async def _ready_client(self, job, item):
        name = item["account"]
        while not job["stop"].is_set():
            if self.account_allowed and not self.account_allowed(name):
                item["note"] = "аккаунт зарезервирован для Автопилота"
                return None
            client = self.get_clients().get(name)
            if client and client.is_connected():
                expected_id = item.get("telegram_id")
                if expected_id is not None and self.account_ids().get(name) != expected_id:
                    item["note"] = "под этим именем подключён другой Telegram-аккаунт"
                    return None
                if not await self._wait_out_quiet(job, item):
                    return client
                break
            if not job.get("resumed") or not self.account_connecting(name):
                return None
            item["note"] = "ожидаю подключения аккаунта"
            try:
                await asyncio.wait_for(job["stop"].wait(), 2)
            except asyncio.TimeoutError:
                pass
        return None

    async def _perform(self, job, item, client, kind, key):
        return await self._join(client, kind, key, self._action_extra(job, item))

    def _action_extra(self, job, item):
        extra = job.get("extra")
        if not job.get("quiet") and not self.account_allowed:
            return extra

        async def wait_before_action():
            if await self._wait_out_quiet(job, item):
                return True
            return self.account_allowed is not None and not self.account_allowed(item["account"])

        return {**(extra or {}), "wait_before_action": wait_before_action}

    async def _wait_for_account(self, job, item, previous=None):
        now = time.time()
        quiet = job.get("quiet")
        due = item.get("until") if item["state"] == "wait" else None
        if due is None:
            base = previous if job.get("resumed") and previous is not None else now
            due = (job["started"] + item["at"] if job.get("finish_at")
                   else plan_options.after_quiet(base + item["base_delay"], quiet))
        item["state"], item["until"] = "wait", due
        job["skip"].clear()
        self.save_jobs()
        if await self._sleep(job, due - now):
            return True
        return await self._wait_out_quiet(job, item)

    async def _wait_out_quiet(self, job, item):
        now = time.time()
        quiet = job.get("quiet")
        while (end := plan_options.quiet_end(now, quiet)) is not None:
            due = plan_options.after_quiet(now, quiet)
            finish = job.get("finish_at")
            if finish is not None and end <= finish:
                due = min(due, finish)
            item["until"] = due
            if item["state"] == "wait":
                self.save_jobs()
            # Acceleration can skip ordinary delays, never the pause itself.
            try:
                await asyncio.wait_for(job["stop"].wait(), due - now)
                return True
            except asyncio.TimeoutError:
                now = time.time()
        item["until"] = None
        return job["stop"].is_set()

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
        """Сократить ожидание текущего аккаунта с учётом времени пропуска."""
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        if job_id:
            running = [j for j in running if j["id"] == job_id]
        if not running:
            raise ValueError("Заказ уже не выполняется")
        for j in running:
            waiting = next((i for i in j["items"] if i["state"] == "wait"), None)
            if waiting is not None:
                now = time.time()
                if plan_options.quiet_end(now, j.get("quiet")) is None:
                    waiting["until"] = now
                j["skip"].set()
        self.save_jobs()
        return {"ok": True}

    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import ImportChatInviteRequest
        try:
            request = (ImportChatInviteRequest(key) if kind == "invite"
                       else JoinChannelRequest(await client.get_entity(key)))
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            await client(request)
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
        finish = plan_options.deadline(plan["finish_at"])
        total = plan_options.schedule_order(items, time.time(), finish, plan["quiet"])
        plan["items"], plan["total"] = items, total
        return self._public_plan(plan)

    def stop(self, job_id=None):
        running = [j for j in self.jobs.values() if j["status"] == "running"]
        if job_id:
            running = [j for j in running if j["id"] == job_id]
        if not running:
            raise ValueError("Нет активной подписки")
        for j in running:
            j["stop_requested"] = True
        self.save_jobs()
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
                "seq": j.get("seq"), "service": j.get("service", ""), "started": j.get("started"), "now": now,
                "finish_at": j.get("finish_at")}

    def public_jobs(self):
        return {"jobs": [self._pub(j) for j in self.jobs.values()]}


def register(app, get_clients, prefix="channels", joiner_cls=None):
    """Добавляет роуты /api/channels/* в aiohttp-приложение (авторизация — ваша auth_mw)."""
    joiner = (joiner_cls or ChannelJoiner)(get_clients)
    joiner.service = prefix
    joiner.account_allowed = app.get(ORDER_ACCOUNT_ALLOWED)
    directory = app.get(ORDER_JOBS_DIRECTORY)
    if directory is not None:
        joiner.load_jobs(directory / (prefix + ".json"))
        joiner.account_connecting = app.get(ORDER_ACCOUNT_CONNECTING, joiner.account_connecting)
        joiner.account_ids = app.get(ORDER_ACCOUNT_IDS, joiner.account_ids)
        app.on_startup.append(joiner.resume_jobs)
        app.on_cleanup.append(joiner.suspend_jobs)

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

    async def available(request):
        d = await body(request)
        d["count"] = max(1, len(joiner._alive()))
        async def calculate():
            try:
                result = await joiner.make_plan(d.get("link"), d["count"], 0, 0, d.get("extra"))
            except ValueError as exc:
                if str(exc).startswith(("Нет подходящих аккаунтов", "Все ", "Ни один из ",
                                        "Нет аккаунтов, подписанных", "Нет подключённых аккаунтов")):
                    return {"count": 0}
                raise
            joiner.cancel(result["id"])
            return {"count": len(result["items"])}
        return await run(calculate)

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
        web.post(f"/api/{prefix}/available", available),
        web.post(f"/api/{prefix}/start", start),
        web.post(f"/api/{prefix}/stop", stop),
        web.post(f"/api/{prefix}/cancel", cancel),
        web.post(f"/api/{prefix}/exclude", exclude),
        web.post(f"/api/{prefix}/skip", skip),
        web.get(f"/api/{prefix}/job", job),
    ])
    if joiner_cls is None:  # подключаем услуги для постов из posts.py
        try:
            import posts
        except ImportError:
            posts = None
        if posts:
            posts.register_all(app, get_clients)
    return joiner
