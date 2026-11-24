"""Compile a restricted scenario language and execute confirmed plans sequentially."""
import ast
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path

from aiohttp import web
from telethon.tl import types

import channels
import multi
import plan_options
import posts
import presence
import unsub

LOG = logging.getLogger(__name__)
MAX_STEPS = 2000
POST_ACTIONS = {
    "views": posts.ViewsJoiner, "react": posts.ReactionJoiner, "poll": posts.PollJoiner,
    "repost": posts.RepostJoiner, "button": multi.ButtonsJoiner,
}
ACTIONS = POST_ACTIONS | {"sub": channels.ChannelJoiner, "unsub": unsub.UnsubJoiner,
                         "start": multi.StartsJoiner, "online": None, "send": None}
ARGUMENTS = {name: ("post",) for name in POST_ACTIONS} | {
    "react": ("post", "emoji"), "poll": ("post", "option"), "sub": ("channel",),
    "unsub": ("channel",), "start": ("bot",), "online": ("seconds",), "send": ("peer", "text"),
}
DEFAULTS = {"emoji": "❤️"}
STATUSES = {"running", "paused", "uncertain"}


def seconds(value, maximum=86400):
    if type(value) not in (int, float) or not 0 <= value <= maximum:
        raise ValueError(f"Время должно быть числом от 0 до {maximum} секунд")
    return value


def account_ids(value):
    if type(value) is int:
        value = [value]
    if not isinstance(value, list) or any(type(item) is not int or not 0 < item < 2**63 for item in value):
        raise ValueError("Укажи Telegram ID или список Telegram ID")
    return list(dict.fromkeys(value))


class Compiler:
    def __init__(self, accounts, groups, params):
        self.accounts = accounts
        self.groups = groups
        self.params = params
        self.steps = []
        self.statements_left = 10000

    def value(self, node, scope):
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool):
            return node.value
        if isinstance(node, ast.List):
            return [self.value(item, scope) for item in node.elts]
        if isinstance(node, ast.Name) and node.id in scope:
            return scope[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            excluded = set(account_ids(self.value(node.right, scope)))
            return [item for item in account_ids(self.value(node.left, scope)) if item not in excluded]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords and len(node.args) == 1:
            name = self.value(node.args[0], scope)
            if isinstance(name, str) and node.func.id == "group" and name in self.groups:
                return self.groups[name]
            if isinstance(name, str) and node.func.id == "param" and name in self.params:
                return self.params[name]
            raise ValueError("Не найдена группа или параметр")
        raise ValueError("Используй переменные, значения, списки, group(…) или param(…)")

    def append(self, step):
        if len(self.steps) >= MAX_STEPS:
            raise ValueError(f"В одном сценарии допускается до {MAX_STEPS} шагов")
        self.steps.append(step)

    def action(self, call, scope, line):
        if not isinstance(call.func, ast.Name):
            raise ValueError("Укажи команду Veltrix")
        name = call.func.id
        values = [self.value(arg, scope) for arg in call.args]
        kwargs = {}
        for kw in call.keywords:
            if kw.arg is None or kw.arg in kwargs:
                raise ValueError("Проверь именованные аргументы")
            kwargs[kw.arg] = self.value(kw.value, scope)
        if name == "wait":
            if kwargs or len(values) not in (1, 2):
                raise ValueError("Используй wait(секунды) или wait(от, до)")
            lo, hi = seconds(values[0]), seconds(values[-1])
            if hi < lo:
                raise ValueError("Верхняя граница паузы меньше нижней")
            self.append({"action": name, "delay": [lo, hi], "line": line})
            return
        if name not in ACTIONS:
            raise ValueError(f"Неизвестная команда {name}")
        fields = ("account",) + ARGUMENTS[name]
        if len(values) > len(fields) or kwargs.keys() - {*fields, "unique"} or kwargs.keys() & set(fields[:len(values)]):
            raise ValueError(f"Аргументы команды {name} — " + ", ".join(fields))
        args = DEFAULTS | dict(zip(fields, values)) | kwargs
        if any(key not in args for key in fields):
            raise ValueError(f"Заполни аргументы {name} — " + ", ".join(fields))
        unique = args.get("unique", False)
        if type(unique) is not bool or unique and name == "online":
            raise ValueError("Укажи unique=True или unique=False для действия с целью")
        data = {key: args[key] for key in ARGUMENTS[name]}
        target = self.validate(name, data)
        for identity in account_ids(args["account"]):
            if identity not in self.accounts:
                raise ValueError(f"Аккаунт {identity} сейчас недоступен")
            self.append({"action": name, "account": identity, "label": self.accounts[identity],
                         "target": target, "args": data, "unique": unique, "line": line})

    @staticmethod
    def validate(name, args):
        for key, value in args.items():
            if key == "option" and type(value) is int and 1 <= value <= 10:
                continue
            if key != "seconds" and (not isinstance(value, str) or not value.strip() or len(value) > 4096):
                raise ValueError(f"Укажи непустой текст для {key}")
        if name in POST_ACTIONS:
            _, (channel, mid) = posts.parse_post_link(args["post"])
            args["post"] = f"https://t.me/{channel.lower()}/{mid}"
            if name == "react" and args["emoji"] not in posts.REACTIONS + ["random"]:
                raise ValueError("Неизвестная реакция")
            if name == "poll" and isinstance(args["option"], str) and not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,32}", args["option"]):
                raise ValueError("Укажи номер варианта от 1 до 10 или его ID в кавычках")
            return args["post"]
        if name in ("sub", "unsub"):
            kind, channel = channels.parse_link(args["channel"])
            return "@" + channel.lower() if kind == "public" else args["channel"]
        if name == "start":
            _, (bot, param) = multi.parse_start_link(args["bot"])
            return "@" + bot.lower() + ("?start=" + param if param else "")
        if name == "send":
            if not re.fullmatch(r"me|@[A-Za-z][A-Za-z0-9_]{2,31}", args["peer"], re.I):
                raise ValueError("Укажи @username получателя либо me для Избранного")
            return args["peer"].lower()
        seconds(args["seconds"], 3600)
        return ""

    def statements(self, statements, scope, depth=0):
        if depth > 1:
            raise ValueError("Вложенные циклы пока не поддерживаются")
        for node in statements:
            self.statements_left -= 1
            if self.statements_left < 0:
                raise ValueError("Слишком сложный сценарий")
            try:
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    name = node.targets[0].id
                    if name == "all":
                        raise ValueError("Сохрани выборку под другим именем, например accounts")
                    scope[name] = self.value(node.value, scope)
                elif isinstance(node, ast.For) and isinstance(node.target, ast.Name) and not node.orelse:
                    if node.target.id == "all":
                        raise ValueError("Укажи имя переменной цикла, например acc")
                    for identity in account_ids(self.value(node.iter, scope)):
                        self.statements(node.body, scope | {node.target.id: identity}, depth + 1)
                elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    self.action(node.value, scope, node.lineno)
                else:
                    raise ValueError("Допустимы присваивания, цикл for и вызовы команд")
            except ValueError as exc:
                if str(exc).startswith("Строка "):
                    raise
                raise ValueError(f"Строка {node.lineno}. {exc}") from None

    def compile(self, code):
        if not isinstance(code, str) or not code.strip() or len(code) > 20000:
            raise ValueError("Введи сценарий до 20 000 символов")
        try:
            tree = ast.parse(code)
            if sum(1 for _ in ast.walk(tree)) > 4000:
                raise ValueError("Слишком сложный сценарий")
            self.statements(tree.body, {"all": list(self.accounts)})
        except SyntaxError as exc:
            raise ValueError(f"Строка {exc.lineno}. Проверь синтаксис и отступы") from None
        except RecursionError:
            raise ValueError("Слишком сложный сценарий") from None
        if not self.steps:
            raise ValueError("Сценарий не содержит действий")
        return self.steps


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Scenarios:
    def __init__(self, directory, accounts):
        self.path = Path(directory) / "security" / "scenarios.json"
        self.accounts = accounts
        self.task = None
        self.membership = channels.ChannelJoiner(lambda: {})
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {"groups": {}, "templates": {}, "journal": {}, "run": None}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(self.state, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(self.path)

    def connected(self):
        return {acc.me.id: (name, acc) for name, acc in self.accounts().items()
                if acc.me is not None and acc.client.is_connected()}

    def plan(self, data):
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Параметры должны быть объектом JSON")
        accounts = {key: " ".join(filter(None, (acc.me.first_name, acc.me.last_name))) or acc.me.username or name
                    for key, (name, acc) in self.connected().items()}
        steps = Compiler(accounts, self.state["groups"], params).compile(data.get("code"))
        quiet = plan_options.quiet_hours(data)
        start = plan_options.deadline(data.get("start_at"))
        return {"steps": steps, "quiet": quiet, "start": start, "stop_on_error": data.get("stop_on_error") is not False}

    def record(self, run, step, status, note):
        run["log"].append({"time": time.time(), "status": status, "note": note[:220],
                           **{key: step[key] for key in ("action", "label", "target", "line") if key in step}})

    async def execute(self, step, name, client, extra):
        action, args = step["action"], step["args"]
        service = ACTIONS[action]
        if action in ("sub", "unsub"):
            kind, key = channels.parse_link(args["channel"])
            if step["unique"]:
                members, errors = await self.membership._subscribed({name: client}, kind, key)
                if errors:
                    return "err", "Не удалось подтвердить подписку"
                if (name in members) == (action == "sub"):
                    return "skip", "Подписка уже в требуемом состоянии"
        elif action in POST_ACTIONS:
            kind, key = posts.parse_post_link(args["post"])
            if action == "poll" and type(args["option"]) is int:
                message = await client.get_messages(key[0], ids=key[1])
                media = getattr(message, "media", None)
                if not isinstance(media, types.MessageMediaPoll) or media.poll.closed:
                    return "err", "Опрос недоступен или закрыт"
                if args["option"] > len(media.poll.answers):
                    return "err", "В опросе нет такого варианта"
                args = args | {"option": media.poll.answers[args["option"] - 1].option.hex()}
        elif action == "start":
            kind, key = multi.parse_start_link(args["bot"])
        elif action == "send":
            peer = await client.get_input_entity(args["peer"])
            if await extra["wait_before_action"]():
                return "skip", "Остановлено"
            await client.send_message(peer, args["text"])
            return "ok", "Сообщение отправлено"
        else:
            if await extra["wait_before_action"]():
                return "skip", "Остановлено"
            try:
                error = await presence.keeper.hold(name, client, src="scenario")
                if error:
                    return "err", error
                until = time.monotonic() + args["seconds"]
                while (self.state["run"]["status"] == "running" and time.monotonic() < until
                       and plan_options.quiet_end(time.time(), self.state["run"]["quiet"]) is None):
                    await asyncio.sleep(min(1, max(0, until - time.monotonic())))
                return "ok", "Сеанс онлайн завершён"
            finally:
                await presence.keeper.release(name, client)
        return await service._join(client, kind, key, args | extra)

    async def worker(self):
        run = self.state["run"]
        while run["status"] == "running" and run["index"] < len(run["steps"]):
            step = run["steps"][run["index"]]
            if step["action"] == "wait" and not run["due"]:
                run["due"] = time.time() + random.uniform(*step["delay"])
                self.save()
            if run["due"] > time.time():
                await asyncio.sleep(min(1, max(0, run["due"] - time.time())))
                continue
            if step["action"] == "wait":
                status, note = "ok", "Пауза завершена"
            else:
                now = time.time()
                if plan_options.quiet_end(now, run["quiet"]) is not None:
                    run["due"] = plan_options.after_quiet(now, run["quiet"])
                    self.save()
                    continue
                entry = self.connected().get(step["account"])
                key = digest([step["account"], step["action"], step["target"],
                              step["args"].get("text")])
                if entry is None:
                    status, note = "err", "Аккаунт сейчас недоступен"
                elif step["unique"] and step["action"] not in ("sub", "unsub") and key in self.state["journal"]:
                    status, note = "skip", "Действие уже выполнено"
                else:
                    run["in_flight"] = True
                    self.save()

                    async def before_action():
                        while run["status"] == "running":
                            now = time.time()
                            if plan_options.quiet_end(now, run["quiet"]) is None:
                                return False
                            until = plan_options.after_quiet(now, run["quiet"])
                            while run["status"] == "running" and time.time() < until:
                                await asyncio.sleep(min(1, max(0, until - time.time())))
                        return True

                    try:
                        status, note = await self.execute(step, entry[0], entry[1].client,
                                                          {"wait_before_action": before_action})
                    except Exception as exc:
                        status, note = posts.explain(exc)
                    run["in_flight"] = False
                    flood = re.search(r"FloodWait: подождать (\d+) сек", note)
                    if status == "err" and flood and run["status"] == "running":
                        run["due"] = time.time() + max(1, int(flood[1]))
                        self.record(run, step, "wait", note)
                        self.save()
                        continue
                    if status == "ok" and step["action"] not in ("online", "sub", "unsub"):
                        self.state["journal"][key] = True
                    if run["status"] != "stopped" and status == "err" and step["action"] in ("send", "button", "start", "repost") and re.search(
                            r"timeout|connection|oserror|network", note, re.I):
                        run["status"] = "uncertain"
                        self.record(run, step, "err", "Результат не подтверждён. Проверь Telegram перед повтором.")
                        self.save()
                        return
                    if status == "skip" and run["status"] == "paused" and note.lower() == "остановлено":
                        self.save()
                        return
            self.record(run, step, status, note)
            run["index"] += 1
            run["due"] = 0
            if status == "err" and run["stop_on_error"] and run["index"] < len(run["steps"]) and run["status"] == "running":
                run["status"] = "paused"
            self.save()
        if run["status"] == "running":
            run["status"] = "finished"
            self.save()

    def launch_worker(self):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.worker())
            self.task.add_done_callback(self.report_failure)

    def report_failure(self, task):
        if task.cancelled() or task.exception() is None:
            return
        LOG.error("Scenario worker failed", exc_info=task.exception())
        run = self.state["run"]
        if run["status"] != "running":
            return
        run.update(status="uncertain" if run["in_flight"] else "paused", in_flight=False)
        if run["index"] < len(run["steps"]):
            self.record(run, run["steps"][run["index"]], "err", "Ошибка выполнения. Проверь журнал панели.")
        try:
            self.save()
        except OSError:
            LOG.exception("Could not persist scenario failure")

    async def lifecycle(self, app):
        run = self.state["run"]
        if run and run["status"] in STATUSES:
            if run["in_flight"]:
                run.update(status="uncertain", in_flight=False)
                self.record(run, run["steps"][run["index"]], "err",
                            "Панель перезапустилась во время шага. Проверь действие перед повтором.")
                self.save()
            elif run["status"] == "running":
                # The web server comes up before Telegram connections.
                run["due"] = max(run["due"], time.time() + 60)
                self.launch_worker()
        yield
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def snapshot(self):
        run = self.state["run"]
        public = None if not run else {key: value for key, value in run.items() if key != "steps"}
        if public is not None:
            public["total"] = len(run["steps"])
        return {"run": public, "groups": self.state["groups"], "templates": self.state["templates"]}

    async def handle(self, request):
        try:
            return await self.dispatch(request)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400, headers={"Cache-Control": "no-store"})

    async def dispatch(self, request):
        command = request.match_info.get("command")
        if request.method == "POST":
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON")
            run = self.state["run"]
            if command in ("preview", "start"):
                plan = self.plan(data)
                token = digest(plan)
                if command == "preview":
                    return web.json_response(plan | {"token": token}, headers={"Cache-Control": "no-store"})
                if token != data.get("token"):
                    raise ValueError("Сценарий или список аккаунтов изменился. Построй предпросмотр заново.")
                if (run and run["status"] in STATUSES) or (self.task and not self.task.done()):
                    raise ValueError("Сначала останови текущий сценарий")
                self.state["run"] = plan | {"index": 0, "due": plan["start"] or 0, "status": "running",
                                            "log": [], "in_flight": False}
                self.save()
                self.launch_worker()
            elif command in ("group", "template"):
                name = data.get("name")
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError("Укажи название до 80 символов")
                collection = self.state["groups" if command == "group" else "templates"]
                if data.get("delete") is True:
                    collection.pop(name, None)
                elif command == "group":
                    collection[name] = account_ids(data.get("ids"))
                else:
                    code = data.get("code")
                    if not isinstance(code, str) or not code.strip() or len(code) > 20000:
                        raise ValueError("Введи сценарий до 20 000 символов")
                    collection[name] = code
                self.save()
            elif command in ("pause", "resume", "skip", "stop"):
                if not run or run["status"] not in STATUSES:
                    raise ValueError("Нет активного сценария")
                if command == "pause" and run["status"] == "running":
                    run["status"] = "paused"
                elif command == "stop":
                    run["status"] = "stopped"
                elif command in ("resume", "skip") and run["status"] in ("paused", "uncertain"):
                    if run["in_flight"]:
                        raise ValueError("Дождись окончания текущего действия")
                    if command == "skip" and run["index"] < len(run["steps"]):
                        self.record(run, run["steps"][run["index"]], "skip", "Пропущено вручную")
                        run["index"] += 1
                        run["due"] = 0
                    run["status"] = "running"
                else:
                    raise ValueError("Команда недоступна для текущего состояния")
                self.save()
                if run["status"] == "running":
                    self.launch_worker()
            else:
                raise web.HTTPNotFound()
        return web.json_response(self.snapshot(), headers={"Cache-Control": "no-store"})

    def register(self, app):
        app.cleanup_ctx.append(self.lifecycle)
        app.router.add_get("/api/scenarios", self.handle)
        app.router.add_post("/api/scenarios/{command}", self.handle)
