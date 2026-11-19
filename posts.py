"""
Вкладки «Реакции» и «Просмотры»: аккаунты по очереди (с задержками) ставят реакцию / засчитывают просмотр
на публичный пост https://t.me/channel/123. Работает на той же схеме, что и «Каналы»
(план -> подтверждение -> несколько параллельных задач), см. channels.py.

Эндпоинты: /api/reactions/{plan,start,stop,cancel,job} и /api/views/{plan,start,stop,cancel,job}
"""
import random
import re

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
        return set(), 0  # для реакций и просмотров подписка не нужна

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


def register_all(app, get_clients):
    channels.register(app, get_clients, prefix="reactions", joiner_cls=ReactionJoiner)
    channels.register(app, get_clients, prefix="views", joiner_cls=ViewsJoiner)
