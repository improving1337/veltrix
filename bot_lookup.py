"""Resolve bots using only the Telegram account that will act."""
import asyncio
from time import monotonic
from weakref import WeakKeyDictionary

from telethon import errors, functions, types

_cache = WeakKeyDictionary()
_locks = WeakKeyDictionary()
MISSING = (errors.UsernameNotOccupiedError, errors.UsernameInvalidError)
LOOKUP_ERRORS = (*MISSING, ValueError)


class BotUnavailable(ValueError):
    pass


def matches_bot(entity, username):
    if not isinstance(entity, types.User) or not entity.bot or entity.deleted:
        return False
    if (entity.username or "").casefold() == username:
        return True
    return any(item.active and item.username.casefold() == username for item in (entity.usernames or ()))


async def find_existing_bot(client, username):
    try:
        peer = client.session.get_input_entity("@" + username)
    except (AttributeError, ValueError):
        peer = None
    if isinstance(peer, types.InputPeerUser):
        try:
            entity = await client.get_entity(peer)
        except (ValueError, errors.PeerIdInvalidError, errors.UserIdInvalidError):
            pass
        else:
            if matches_bot(entity, username):
                return entity

    result = await client(functions.contacts.SearchRequest(q=username, limit=10, bots=True))
    for user in getattr(result, "users", ()):
        if matches_bot(user, username):
            return user

    for dialog in await client.get_dialogs(limit=100):
        if matches_bot(dialog.entity, username):
            return dialog.entity
    return None


async def resolve_bot(client, value):
    username = value.strip().lstrip("@").casefold()
    lock = _locks.setdefault(client, asyncio.Lock())
    async with lock:
        entries = _cache.setdefault(client, {})
        cached = entries.get(username)
        if cached and monotonic() < cached[0]:
            entity = cached[1]
        else:
            try:
                entity = await client.get_entity(username)
            except LOOKUP_ERRORS as exc:
                missing = isinstance(exc, MISSING) or isinstance(exc.__cause__, MISSING) or (
                    str(exc).startswith("No user has ") and str(exc).endswith(" as username"))
                if not missing:
                    raise
                entity = await find_existing_bot(client, username)
            else:
                if not getattr(entity, "bot", False) or getattr(entity, "deleted", False):
                    raise ValueError(f"@{username} не является ботом")
            entries[username] = (monotonic() + (120 if entity else 30), entity)
        if entity is None:
            raise BotUnavailable(f"Telegram не нашёл бота @{username} для этой сессии")
        return entity
