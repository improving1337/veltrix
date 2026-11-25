"""
Услуги «Реакции», «Просмотры» и «Репосты»: аккаунты по очереди выполняют действие
с публичным постом https://t.me/channel/123. Работает на той же схеме, что и «Каналы»
(план -> подтверждение -> несколько параллельных задач), см. channels.py.

Префиксы API: /api/reactions, /api/views, /api/reposts
"""
import random
import re

from telethon.errors import ChatForwardsRestrictedError

import channels
import plan_options
from channels import ChannelJoiner

REACTIONS = ["❤️", "👍", "👎", "🔥", "😭", "🤬", "😍", "💔", "🤡", "💩", "🎉", "😁", "👏", "🤩", "💯", "👌", "🙏"]


def parse_post_link(link):
    """-> ('post', (channel, message_id))"""
    s = (link or "").strip()
    if not s:
        raise ValueError("Укажи ссылку на пост")
    if re.search(r"(?:t\.me|telegram\.me)/c/", s, re.I):
        raise ValueError("Нужна ссылка на публичный пост (t.me/channel/123), а не t.me/c/…")
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z][\w]{3,})/(\d+)", s, re.I)
    if not m:
        raise ValueError("Нужна ссылка на пост, например https://t.me/cs2lab/123")
    return "post", (m.group(1), int(m.group(2)))


def explain(e):
    n = type(e).__name__
    if n == "FloodWaitError":
        return "err", f"FloodWait: подождать {getattr(e, 'seconds', '?')} сек"
    if n == "ReactionInvalidError":
        return "err", "эта реакция недоступна в канале"
    if n in ("MessageIdInvalidError", "ChannelPrivateError", "ChannelInvalidError"):
        return "err", "пост недоступен для аккаунта"
    if n in ("UsernameNotOccupiedError", "UsernameInvalidError"):
        return "err", "канал не найден"
    return "err", f"{n}: {e}"[:200]


class PostBase(ChannelJoiner):
    parse = staticmethod(parse_post_link)

    async def _subscribed(self, alive, kind, key):
        return set(), 0  # для действий с публичным постом подписка не нужна

    async def _title(self, client, kind, key):
        """Проверяем, что пост существует; возвращаем «канал · пост N»."""
        channel, mid = key
        try:
            ent = await client.get_entity(channel)
            msg = await client.get_messages(ent, ids=mid)
        except Exception as e:
            n = type(e).__name__
            if n in ("UsernameNotOccupiedError", "UsernameInvalidError"):
                raise ValueError("Канал с таким username не найден")
            return None  # не смогли проверить — не мешаем строить план
        if not msg:
            raise ValueError("Пост не найден или недоступен")
        return f"{getattr(ent, 'title', None) or channel} · пост {mid}"


class ReactionJoiner(PostBase):
    async def make_plan(self, link, count, dmin, dmax, extra=None):
        emoji = (extra or {}).get("emoji") or REACTIONS[0]
        if emoji != "random" and emoji not in REACTIONS:
            raise ValueError("Неизвестная реакция")
        return await super().make_plan(link, count, dmin, dmax, {**(extra or {}), "emoji": emoji})

    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        channel, mid = key
        emoji = (extra or {}).get("emoji") or REACTIONS[0]
        if emoji == "random":
            emoji = random.choice(REACTIONS)
        try:
            ent = await client.get_entity(channel)
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            await client(SendReactionRequest(peer=ent, msg_id=mid, reaction=[ReactionEmoji(emoticon=emoji)]))
            return "ok", f"реакция {emoji}"
        except Exception as e:
            return explain(e)


class ViewsJoiner(PostBase):
    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.messages import GetMessagesViewsRequest
        channel, mid = key
        try:
            ent = await client.get_entity(channel)
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            r = await client(GetMessagesViewsRequest(peer=ent, id=[mid], increment=True))
            n = r.views[0].views if getattr(r, "views", None) else None
            return "ok", "просмотр засчитан" + (f" (всего {n})" if n is not None else "")
        except Exception as e:
            return explain(e)


class RepostJoiner(PostBase):
    @staticmethod
    async def _join(client, kind, key, extra=None):
        channel, message_id = key
        try:
            source = await client.get_input_entity(channel)
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            message = await client.forward_messages("me", message_id, from_peer=source)
            if not message:
                return "err", "Telegram не подтвердил пересылку"
            return "ok", "переслано в «Избранное»"
        except ChatForwardsRestrictedError:
            return "err", "Telegram запретил пересылку из этого канала"
        except Exception as e:
            return explain(e)


class PollJoiner(PostBase):
    async def options(self, link):
        from telethon.tl import types
        _, (channel, mid) = self.parse(link)
        client = next(iter(self._alive().values()), None)
        if client is None:
            raise ValueError("Нет подключённых аккаунтов")
        message = await client.get_messages(channel, ids=mid)
        media = getattr(message, "media", None)
        if not isinstance(media, types.MessageMediaPoll):
            raise ValueError("В этом посте нет опроса")
        if media.poll.closed:
            raise ValueError("Опрос уже закрыт")
        return {"question": media.poll.question.text,
                "options": [{"id": option.option.hex(), "text": option.text.text}
                            for option in media.poll.answers]}

    async def make_plan(self, link, count, dmin, dmax, extra=None):
        extra = extra or {}
        votes = extra.get("votes")
        if not isinstance(votes, dict):
            raise ValueError("Узнайте варианты опроса и задайте количество голосов")
        poll = await self.options(link)
        labels = {option["id"]: option["text"] for option in poll["options"]}
        if any(option not in labels or type(number) is not int or number < 0 for option, number in votes.items()):
            raise ValueError("Укажите целое неотрицательное число голосов для каждого варианта")
        count = sum(votes.values())
        if count < 1:
            raise ValueError("Задайте хотя бы один голос")
        public = await super().make_plan(link, count, dmin, dmax, extra.copy())
        plan = self.plans[public["id"]]
        size = len(plan["items"])
        raw = {option: number * size for option, number in votes.items()}
        allocation = {option: value // count for option, value in raw.items()}
        left = size - sum(allocation.values())
        for option in sorted(votes, key=lambda key: -(raw[key] % count))[:left]:
            allocation[option] += 1
        selected = [option for option, number in allocation.items() for _ in range(number)]
        random.shuffle(selected)
        plan["extra"]["votes_by_account"] = dict(zip((item["account"] for item in plan["items"]), selected))
        plan["poll_labels"] = labels
        return self._public_plan(plan)

    @staticmethod
    def _public_plan(plan):
        result = PostBase._public_plan(plan)
        votes = plan.get("extra", {}).get("votes_by_account", {})
        labels = plan.get("poll_labels", {})
        for item in result["items"]:
            item["option"] = labels.get(votes.get(item["account"]), "")
        return result

    def _action_extra(self, job, item):
        extra = super()._action_extra(job, item)
        return {**extra, "option": extra["votes_by_account"][item["account"]]}

    @staticmethod
    async def _join(client, kind, key, extra=None):
        from telethon.tl.functions.messages import SendVoteRequest
        from telethon.tl.types import MessageMediaPoll
        channel, mid = key
        try:
            source = await client.get_input_entity(channel)
            if 'poll_id' in (extra or {}):
                message = await client.get_messages(source, ids=mid)
                media = getattr(message, 'media', None)
                if not isinstance(media, MessageMediaPoll) or str(media.poll.id) != extra['poll_id'] or media.poll.closed:
                    return "err", "опрос закрыт или недоступен"
                if any(answer.chosen for answer in (media.results.results or [])):
                    return "err", "аккаунт уже проголосовал"
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            await client(SendVoteRequest(source, mid, [bytes.fromhex(extra["option"])]))
            return "ok", "голос отправлен"
        except Exception as exc:
            return explain(exc)


def register_all(app, get_clients):
    channels.register(app, get_clients, prefix="reactions", joiner_cls=ReactionJoiner)
    channels.register(app, get_clients, prefix="views", joiner_cls=ViewsJoiner)
    channels.register(app, get_clients, prefix="reposts", joiner_cls=RepostJoiner)
    poll = channels.register(app, get_clients, prefix="polls", joiner_cls=PollJoiner)

    async def options(request):
        from aiohttp import web
        data = await request.json()
        try:
            return web.json_response(await poll.options(data.get("link")))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app.router.add_post("/api/polls/options", options)
