"""
Услуги «Уч», «Саб+Уч» и «Старты». Работают на той же схеме, что «Каналы» (channels.py):
план -> подтверждение -> задача с задержками между аккаунтами.

Уч: каждый аккаунт по очереди
    0. выходит в сеть
    1. подписка на каналы из условий
    2. просмотр поста
    3. реакция на пост
    4. нажатие первой кнопки под постом (как во вкладке «Кнопки»)
    5. выходит из сети (всегда, даже если задачу остановили или шаг упал с ошибкой)
Старты: каждый аккаунт запускает бота по ссылке t.me/bot?start=param.

Эндпоинты: /api/multi/{plan,start,stop,cancel,job} и /api/starts/{plan,start,stop,cancel,job}
"""
import asyncio
import random
import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

import bot_lookup
import channels
import plan_options
import presence
from channels import ChannelJoiner, parse_link
from posts import REACTIONS, ReactionJoiner, ViewsJoiner, explain, parse_post_link


# ======================= МУЛЬТИ =======================
async def click_first_button(client, channel, mid, extra=None):
    """Нажимает первую кнопку [0, 0] под постом. -> (state, note)"""
    from telethon.errors import BotResponseTimeoutError
    try:
        ent = await client.get_entity(channel)
        msg = await client.get_messages(ent, ids=mid)
        if not msg:
            return "err", "пост не найден или недоступен"
        if not msg.buttons:
            return "ok", "у поста нет кнопок, пропущено"
        if await plan_options.wait_before_action(extra):
            return "skip", "остановлено"
        res = await msg.click(0, 0)
    except BotResponseTimeoutError:
        return "ok", "кнопка нажата, бот не ответил"
    except Exception as e:
        return explain(e)
    if isinstance(res, str):
        return "ok", "кнопка-ссылка: " + res
    text = getattr(res, "message", None)
    return "ok", "кнопка нажата" + (f": {text}" if text else "")


def subscription_targets(raw_links):
    channels_, seen = [], set()
    for raw in raw_links:
        link = str(raw).strip()
        if not link:
            continue
        kind, key = parse_link(link)
        identity = (kind, key.lower() if kind == "public" else key)
        if identity not in seen:
            seen.add(identity)
            channels_.append({"link": link, "kind": kind, "key": key})
    return channels_


def reaction_accounts(items, percent):
    total = len(items)
    count = (total * percent + 50) // 100
    return [item["account"] for index, item in enumerate(items, 1)
            if index * count // total != (index - 1) * count // total]


class MultiJoiner(ChannelJoiner):
    parse = staticmethod(parse_post_link)

    async def make_plan(self, link, count, dmin, dmax, extra=None):
        _, key = self.parse(link)
        channel, mid = key
        extra = extra or {}
        percent = extra.get("reaction_percent", 100)
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("Доля реакций должна быть целым числом от 0 до 100")
        emoji = extra.get("emoji") or REACTIONS[0]
        if emoji != "random" and emoji not in REACTIONS:
            raise ValueError("Неизвестная реакция")
        chans = subscription_targets(extra.get("channels") or [])
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

        # «только уникальные аккаунты»: аккаунты, которые уже подписаны на ВСЕ каналы из списка, не берём
        unique = bool(extra.get("unique"))
        already, check_errors, pool = [], 0, alive
        if unique:
            if not chans:
                raise ValueError("Для режима «только уникальные аккаунты» укажи хотя бы один канал")
            common = None
            for c in chans:
                sub, err = await self._subscribed(alive, c["kind"], c["key"])
                check_errors = max(check_errors, err)
                common = sub if common is None else common & sub
            already = sorted(common or ())
            pool = {n: cl for n, cl in alive.items() if n not in set(already)}
            if not pool:
                raise ValueError(f"Все {len(alive)} подключённых аккаунтов уже подписаны на все указанные каналы")

        # проверяем пост и смотрим, есть ли под ним кнопки
        client = next(iter(pool.values()))
        title, button = None, None
        try:
            ent = await client.get_entity(channel)
            msg = await client.get_messages(ent, ids=mid)
        except Exception as e:
            if type(e).__name__ in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                raise ValueError("Канал с таким username не найден")
            ent = msg = None
        if ent is not None:
            if not msg:
                raise ValueError("Пост не найден или недоступен")
            title = f"{getattr(ent, 'title', None) or channel} · пост {mid}"
            if msg.buttons:
                button = msg.buttons[0][0].text

        requested = count
        count = min(count, len(pool))
        names = random.sample(sorted(pool), count)
        items, at = [], 0
        for n in names:
            d = random.randint(dmin, dmax)
            at += d
            items.append({"account": n, "delay": d, "at": at})

        steps = ["выход в сеть"]
        if chans:
            steps.append(f"подписка на каналы: {len(chans)}" + (" (только уникальные аккаунты)" if unique else ""))
        steps.append("просмотр поста")
        if percent:
            steps.append(f"реакция {'случайная' if emoji == 'random' else emoji} · {percent}%")
        steps.append(f"кнопка «{button}»" if button else "кнопка (под постом кнопок нет — шаг будет пропущен)")
        steps.append("выход из сети")

        pid = uuid.uuid4().hex
        plan = {"id": pid, "link": link.strip(), "kind": "post", "key": key, "title": title,
                "items": items, "total": at, "created": time.time(), "alive": len(alive),
                "requested": requested, "already": already, "check_errors": check_errors, "steps": steps,
                "extra": {"emoji": emoji, "channels": chans, "unique": unique, "reaction_percent": percent}}
        return self.store_plan(plan, extra)

    @staticmethod
    def _public_plan(p):
        out = ChannelJoiner._public_plan(p)
        percent = p["extra"]["reaction_percent"]
        chosen = set(reaction_accounts(p["items"], percent))
        for item in out["items"]:
            item["reaction_planned"] = item["account"] in chosen
        out.update(steps=p.get("steps", []), reaction_count=len(chosen), reaction_percent=percent)
        return out

    async def start(self, plan_id):
        plan = self.plans.get(plan_id)
        if plan:
            extra = plan["extra"]
            extra["reaction_accounts"] = reaction_accounts(plan["items"], extra["reaction_percent"])
        return await super().start(plan_id)

    async def _perform(self, job, item, client, kind, key):
        try:
            return await self._account(job, item, client, key)
        except Exception as exc:
            return "err", f"{type(exc).__name__}: {exc}"[:200]

    async def _account(self, job, it, client, key):
        """Аккаунт выходит в сеть -> делает все действия -> выходит из сети. -> (state, note)"""
        it["note"] = "выходит в сеть…"
        err = await presence.keeper.hold(it["account"], client, "multi")
        pres_notes = [f"✗ не удалось выйти в сеть ({err})"] if err else []
        try:
            if not err:
                try:   # небольшая пауза «после входа», чтобы действия не шли в ту же секунду
                    await asyncio.wait_for(job["stop"].wait(), random.uniform(1.0, 3.0))
                except asyncio.TimeoutError:
                    pass
            state, note = await self._steps(job, it, client, key)
        finally:
            # выходим из сети всегда: и после остановки задачи, и после ошибки
            # (если аккаунт параллельно сидит в сети по фоновому расписанию — он останется в сети до конца захода)
            off_err = await presence.keeper.release(it["account"], client)
        if off_err:
            pres_notes.append(f"✗ не удалось выйти из сети ({off_err})")
        return state, " · ".join([note] + pres_notes if note else pres_notes)

    async def _steps(self, job, it, client, key):
        """Действия одного аккаунта (пока он в сети). -> (state, note)"""
        extra = self._action_extra(job, it) or {}
        notes, failed, stop_all = [], False, False

        def log(res):
            nonlocal failed, stop_all
            ok, text = res
            notes.append(("✓ " if ok == "ok" else "✗ ") + text)
            if ok != "ok":
                failed = True
            if "FloodWait" in text:
                stop_all = True   # Telegram просит подождать — остальные шаги этого аккаунта не делаем
            it["note"] = " · ".join(notes)

        async def pause():
            """Небольшая пауза между шагами; True — задачу остановили."""
            try:
                await asyncio.wait_for(job["stop"].wait(), random.uniform(0.7, 2.0))
                return True
            except asyncio.TimeoutError:
                return False

        # 1. подписка на каналы
        chans = extra.get("channels") or []
        joined = 0
        for i, ch in enumerate(chans):
            if job["stop"].is_set():
                break
            it["note"] = f"подписывается ({i + 1}/{len(chans)})…"
            state, note = await ChannelJoiner._join(client, ch["kind"], ch["key"], extra)
            if state == "ok":
                joined += 1
            else:
                log((state, f"{ch['link']}: {note}"))
            if stop_all:
                break
            if i < len(chans) - 1 and await pause():
                break
        if chans and joined == len(chans):
            log(("ok", f"подписка {joined}/{len(chans)}"))

        # Handle each post in order, completing its actions before the next one.
        posts = extra.get("posts")
        chosen = extra.get("reaction_accounts")
        for target in posts or [key]:
            channel, mid = target
            react = (random.randrange(100) < extra["reaction_percent"] if posts
                     else chosen is None or it["account"] in chosen)
            steps = [("смотрит пост…", lambda: ViewsJoiner._join(client, "post", target, extra))]
            if react:
                steps.append(("ставит реакцию…", lambda: ReactionJoiner._join(client, "post", target, extra)))
            steps.append(("нажимает кнопку…", lambda: click_first_button(client, channel, mid, extra)))
            for label, fn in steps:
                if stop_all or job["stop"].is_set() or await pause():
                    break
                it["note"] = " · ".join(notes + [label])
                log(await fn())
            if stop_all or job["stop"].is_set():
                break
        if job["stop"].is_set() or stop_all:
            notes.append("прервано")
        return ("err" if failed else "ok"), " · ".join(notes)


class SubEntryJoiner(MultiJoiner):
    """Subscribe to channels first, then visit posts with buttons in order."""

    async def make_plan(self, link, count, dmin, dmax, extra=None):
        extra = extra or {}
        channels = extra.get("channels")
        post_links = extra.get("post_links")
        for values, label in ((channels, "каналы"), (post_links, "посты с кнопками")):
            if not isinstance(values, list) or not 1 <= len(values) <= 30:
                raise ValueError(f"Укажите {label}: от 1 до 30 ссылок")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"Проверьте список «{label}»")
        subscriptions = subscription_targets(channels)
        posts = [(channel, mid) for _, (channel, mid) in map(parse_post_link, post_links)]
        percent = extra.get("reaction_percent", 0)
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("Шанс реакции должен быть целым числом от 0 до 100")
        emoji = extra.get("emoji") or REACTIONS[0]
        if emoji != "random" and emoji not in REACTIONS:
            raise ValueError("Неизвестная реакция")
        try:
            count, dmin, dmax = int(count), int(dmin), int(dmax)
        except (TypeError, ValueError) as exc:
            raise ValueError("Количество и задержки должны быть числами") from exc
        if count < 1 or dmin < 0 or dmax < dmin:
            raise ValueError("Укажите хотя бы один аккаунт и корректный диапазон задержек")
        for plan_id, plan in list(self.plans.items()):
            if time.time() - plan["created"] > 600:
                self.plans.pop(plan_id)
        alive = await self.eligible(extra)
        if not alive:
            raise ValueError("Нет подключённых аккаунтов")
        items = [{"account": name, "delay": random.randint(dmin, dmax)}
                 for name in random.sample(sorted(alive), min(len(alive), count))]
        steps = ["Подписка · " + item["link"] for item in subscriptions]
        steps.extend(f"Просмотр и первая кнопка · https://t.me/{channel}/{mid}" for channel, mid in posts)
        plan = {"id": uuid.uuid4().hex, "link": post_links[0].strip(), "kind": "post", "key": posts[0],
                "title": f"{len(subscriptions)} каналов · {len(posts)} постов с кнопками",
                "items": items, "created": time.time(), "alive": len(alive), "requested": count,
                "steps": steps, "extra": {"channels": subscriptions, "posts": posts,
                                         "emoji": emoji, "reaction_percent": percent}}
        return self.store_plan(plan, extra)

    @staticmethod
    def _public_plan(plan):
        result = ChannelJoiner._public_plan(plan)
        result.update(steps=plan["steps"], reaction_chance=plan["extra"]["reaction_percent"])
        return result

    async def start(self, plan_id):
        return await ChannelJoiner.start(self, plan_id)


# ======================= MINI APP =======================
def parse_miniapp_link(link):
    """-> (username, short_name | '', start_param). Ссылки: t.me/bot/app?startapp=x, t.me/bot?startapp=x, tg://resolve?domain=bot&appname=app&startapp=x"""
    s = (link or "").strip()
    if not s:
        raise ValueError("Укажи ссылку на мини-приложение")
    p = urlparse(s if "://" in s else "https://" + s)
    q = parse_qs(p.query)
    short = ""
    if p.scheme == "tg" and p.netloc == "resolve":
        username, short = (q.get("domain") or [""])[0], (q.get("appname") or [""])[0]
    elif p.netloc.lower().replace("www.", "") in ("t.me", "telegram.me"):
        parts = [x for x in p.path.split("/") if x]
        username = parts[0] if parts else ""
        short = parts[1] if len(parts) > 1 else ""
    else:
        raise ValueError("Нужна ссылка вида https://t.me/bot/app?startapp=param")
    if not re.fullmatch(r"[A-Za-z]\w{2,}", username or ""):
        raise ValueError("Не удалось найти username бота в ссылке")
    if short and not re.fullmatch(r"[A-Za-z]\w{1,}", short):
        raise ValueError("Некорректное имя приложения в ссылке")
    param = (q.get("startapp") or [""])[0]
    if not re.fullmatch(r"[\w-]{0,512}", param):
        raise ValueError("Недопустимый параметр startapp")
    return username, short, param


# ======================= СТАРТЫ =======================
def parse_start_link(link):
    """-> ('bot', (username, start_param))"""
    s = (link or "").strip()
    if not s:
        raise ValueError("Укажи ссылку на бота")
    m = re.fullmatch(r"@?([A-Za-z]\w{2,})", s)
    if m:
        username, q = m.group(1), {}
    else:
        p = urlparse(s if "://" in s else "https://" + s)
        q = parse_qs(p.query)
        if p.scheme == "tg" and p.netloc == "resolve":
            username = (q.get("domain") or [""])[0]
        elif p.netloc.lower().replace("www.", "") in ("t.me", "telegram.me"):
            username = p.path.strip("/").split("/")[0]
        else:
            raise ValueError("Нужна ссылка вида https://t.me/bot?start=param")
    if not re.fullmatch(r"[A-Za-z]\w{2,}", username or ""):
        raise ValueError("Не удалось найти username бота в ссылке")
    if "startapp" in q:
        raise ValueError("Ссылки startapp (мини-приложения) не поддерживаются — нужна ссылка с ?start=")
    param = (q.get("start") or [""])[0]
    if not re.fullmatch(r"[\w-]{0,64}", param):
        raise ValueError("Недопустимый параметр start")
    return "bot", (username, param)


class StartsJoiner(ChannelJoiner):
    parse = staticmethod(parse_start_link)
    ALREADY_ALL = "Все {n} подключённых аккаунтов уже запускали этого бота"

    async def _title(self, client, kind, key):
        username, param = key
        try:
            ent = await bot_lookup.resolve_bot(client, username)
        except (bot_lookup.BotUnavailable, ConnectionError, OSError):
            return None
        if not getattr(ent, "bot", False):
            raise ValueError("Это не бот")
        return f"{getattr(ent, 'first_name', None) or username} (@{username})" + (f" · start={param}" if param else "")

    async def _subscribed(self, alive, kind, key):
        """Аккаунты, у которых уже есть переписка с ботом, в план не берём:
        реферальный /start засчитывается только при первом запуске."""
        username, _ = key
        sem = asyncio.Semaphore(8)

        async def one(client):
            async with sem:
                try:
                    ent = await bot_lookup.resolve_bot(client, username)
                    return bool(await client.get_messages(ent, limit=1))
                except Exception as e:
                    print(f"[STARTS] не удалось проверить переписку: {type(e).__name__}: {e}"[:200], flush=True)
                    return None

        names = sorted(alive)
        res = await asyncio.gather(*(one(alive[n]) for n in names))
        print(f"[STARTS] уже запускали: {sum(r is True for r in res)}, новые: {sum(r is False for r in res)}, "
              f"не удалось проверить: {sum(r is None for r in res)}", flush=True)
        return {n for n, r in zip(names, res) if r is True}, sum(1 for r in res if r is None)

    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.messages import StartBotRequest
        username, param = key
        try:
            try:
                ent = await bot_lookup.resolve_bot(client, username)
            except bot_lookup.BotUnavailable as exc:
                if (extra or {}).get("skip_unavailable_bot"):
                    return "bot_unavailable", str(exc)
                return "err", str(exc)
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            if param:
                await client(StartBotRequest(bot=ent, peer=ent, random_id=random.randrange(1 << 62), start_param=param))
            else:
                await client.send_message(ent, "/start")
            return "ok", "бот запущен" + (f" (start={param})" if param else "")
        except Exception as e:
            n = type(e).__name__
            if n in ("YouBlockedUserError", "UserIsBlockedError"):
                return "err", "аккаунт заблокировал этого бота"
            if n == "BotInvalidError":
                return "err", "бот недоступен"
            return explain(e)


# ======================= КНОПКИ =======================
class ButtonsJoiner(ChannelJoiner):
    """Нажатие первой кнопки под постом. Если указаны каналы — в план берутся только аккаунты,
    подписанные на все эти каналы. Аккаунты, которые уже нажимали эту кнопку, повторно не берутся."""
    parse = staticmethod(parse_post_link)
    ALREADY_ALL = "Все подходящие аккаунты уже нажимали эту кнопку"

    def __init__(self, get_clients):
        super().__init__(get_clients)
        self.used = {}   # (channel, message_id) -> {имена аккаунтов}

    async def make_plan(self, link, count, dmin, dmax, extra=None):
        _, key = self.parse(link)
        channel, mid = key
        chans, seen = [], set()
        for raw in (extra or {}).get("channels") or []:
            raw = str(raw).strip()
            if not raw:
                continue
            kind, k = parse_link(raw)
            if kind != "public":
                raise ValueError(f"{raw}: для проверки подписки нужна ссылка на публичный канал (не invite)")
            if k.lower() not in seen:
                seen.add(k.lower())
                chans.append(k)
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

        # кто подписан на все нужные каналы
        sem, errors = asyncio.Semaphore(8), 0

        async def member(name, client):
            nonlocal errors
            async with sem:
                try:
                    from telethon.tl.functions.channels import GetParticipantRequest
                    from telethon.tl.types import InputPeerSelf
                    for ch in chans:
                        ent = await client.get_entity(ch)
                        try:
                            await client(GetParticipantRequest(ent, InputPeerSelf()))
                        except Exception as e:
                            if type(e).__name__ in ("UserNotParticipantError", "ChannelPrivateError"):
                                return name, False
                            raise
                    return name, True
                except Exception as e:
                    errors += 1
                    print(f"[BUTTONS] проверка подписки {name}: {type(e).__name__}: {e}"[:200], flush=True)
                    return name, False

        if chans:
            eligible = [n for n, ok in await asyncio.gather(*(member(n, c) for n, c in alive.items())) if ok]
        else:
            eligible = list(alive)
        if not eligible:
            raise ValueError("Нет аккаунтов, подписанных на все указанные каналы")
        used = self.used.get(key, set())
        free = sorted(n for n in eligible if n not in used)
        already = sorted(n for n in eligible if n in used)
        if not free:
            raise ValueError(self.ALREADY_ALL)

        # проверяем пост и читаем кнопку
        client = alive[random.choice(free)]
        button = None
        try:
            ent = await client.get_entity(channel)
            msg = await client.get_messages(ent, ids=mid)
        except Exception as e:
            if type(e).__name__ in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                raise ValueError("Канал с таким username не найден")
            raise ValueError(f"Не удалось прочитать пост: {type(e).__name__}")
        if not msg:
            raise ValueError("Пост не найден или недоступен")
        if not msg.buttons:
            raise ValueError("У поста нет кнопок")
        button = msg.buttons[0][0].text
        title = f"{getattr(ent, 'title', None) or channel} · пост {mid}"

        requested = count
        count = min(count, len(free))
        names = random.sample(free, count)
        items, at = [], 0
        for n in names:
            d = random.randint(dmin, dmax)
            at += d
            items.append({"account": n, "delay": d, "at": at})
        pid = uuid.uuid4().hex
        plan = {"id": pid, "link": link.strip(), "kind": "post", "key": key, "title": title, "items": items,
                "total": at, "created": time.time(), "alive": len(eligible), "requested": requested,
                "already": already, "check_errors": errors, "steps": [f"кнопка «{button}»"], "extra": None}
        return self.store_plan(plan, extra)

    @staticmethod
    def _public_plan(p):
        out = ChannelJoiner._public_plan(p)
        out["steps"] = p.get("steps", [])
        return out

    async def start(self, plan_id):
        plan = self.plans.get(plan_id)
        res = await super().start(plan_id)
        if plan:
            self.used.setdefault(plan["key"], set()).update(i["account"] for i in plan["items"])
        return res

    @staticmethod
    async def _join(client, kind, key, extra=None):
        channel, mid = key
        return await click_first_button(client, channel, mid, extra)


def register_all(app, get_clients):
    channels.register(app, get_clients, prefix="multi", joiner_cls=MultiJoiner)
    channels.register(app, get_clients, prefix="subentry", joiner_cls=SubEntryJoiner)
    channels.register(app, get_clients, prefix="starts", joiner_cls=StartsJoiner)
    channels.register(app, get_clients, prefix="btn", joiner_cls=ButtonsJoiner)
