"""
Услуга «Отписки»: аккаунты по очереди (с задержками) выходят из канала.

Схема та же, что у «Подписчиков» (channels.py): план -> подтверждение -> задача.
При построении плана проверяется, какие аккаунты сейчас подписаны на канал — только они
попадают в план (не подписанные и те, кого не удалось проверить, исключаются).

Эндпоинты: /api/unsub/{plan,start,stop,cancel,exclude,skip,job}
"""
import channels
import plan_options
from channels import ChannelJoiner


class UnsubJoiner(ChannelJoiner):
    # parse = parse_link (по умолчанию): t.me/channel, @channel или инвайт-ссылка

    def _candidates(self, alive, subscribed):
        """Наоборот к подписке: в план берём только тех, кто сейчас в канале."""
        if not subscribed:
            raise ValueError(f"Ни один из {len(alive)} подключённых аккаунтов не подписан на этот канал")
        return sorted(subscribed), sorted(n for n in alive if n not in subscribed)

    @staticmethod
    async def _join(client, kind, key, extra=None):
        """Выход из канала/группы. -> (state, note)"""
        from telethon.tl import types
        from telethon.tl.functions.channels import LeaveChannelRequest
        from telethon.tl.functions.messages import CheckChatInviteRequest, DeleteChatUserRequest
        try:
            if kind == "invite":
                r = await client(CheckChatInviteRequest(key))
                ent = getattr(r, "chat", None) if isinstance(r, types.ChatInviteAlready) else None
                if ent is None:
                    return "ok", "уже не в канале"
            else:
                ent = await client.get_entity(key)
            request = (DeleteChatUserRequest(ent.id, types.InputUserSelf()) if isinstance(ent, types.Chat)
                       else LeaveChannelRequest(await client.get_input_entity(ent)))
            if await plan_options.wait_before_action(extra):
                return "skip", "остановлено"
            await client(request)
            return "ok", "вышел из канала"
        except Exception as e:
            n = type(e).__name__
            if n in ("UserNotParticipantError", "ChannelPrivateError", "PeerIdInvalidError"):
                return "ok", "уже не в канале"
            if n == "UserCreatorError":
                return "err", "аккаунт — владелец канала, выйти нельзя"
            if n == "FloodWaitError":
                return "err", f"FloodWait: подождать {getattr(e, 'seconds', '?')} сек"
            return "err", f"{n}: {e}"[:200]


def register_all(app, get_clients):
    channels.register(app, get_clients, prefix="unsub", joiner_cls=UnsubJoiner)
