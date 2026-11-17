
import asyncio
import re

from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.tl.functions.messages import (
    SendReactionRequest,
    GetMessagesViewsRequest,
)
from telethon.tl.types import ReactionEmoji

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION_NAME = "session/6283865292100.session"


def parse_telegram_url(url):
    url = url.strip().rstrip("/")

    match = re.fullmatch(
        r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)",
        url,
    )

    if not match:
        raise ValueError(
            "Нужна ссылка на публичный пост, например "
            "https://t.me/cs2lab/123"
        )

    return match.group(1), int(match.group(2))


async def main():
    url = input("Вставь ссылку на пост Telegram: ")

    try:
        channel, message_id = parse_telegram_url(url)
    except ValueError as error:
        print(error)
        return

    async with TelegramClient(
        SESSION_NAME.removesuffix(".session"),
        API_ID,
        API_HASH,
    ) as client:
        try:
            entity = await client.get_entity(channel)

            # Получаем конкретный пост из истории канала
            message = None

            async for item in client.iter_messages(
                entity,
                limit=1,
                min_id=message_id - 1,
                max_id=message_id + 1,
            ):
                if item.id == message_id:
                    message = item
                    break

            if not message:
                print("Пост не найден или недоступен.")
                return

            print("Канал:", getattr(entity, "title", channel))
            print("ID поста:", message.id)
            print("Текст:", message.text or "(нет текста)")

            # Запрашиваем учёт просмотра
            try:
                await client(
                    GetMessagesViewsRequest(
                        peer=entity,
                        id=[message_id],
                        increment=True,
                    )
                )
                print("Запрос на учёт просмотра отправлен.")
            except RPCError as error:
                print("Ошибка учёта просмотра:", error)

            # Ставим реакцию ❤️
            try:
                await client(
                    SendReactionRequest(
                        peer=entity,
                        msg_id=message_id,
                        reaction=[
                            ReactionEmoji(emoticon="❤️")
                        ],
                    )
                )
                print("Реакция ❤️ поставлена.")
            except RPCError as error:
                print("Ошибка реакции:", error)

        except RPCError as error:
            print("Ошибка Telegram API:", error)
        except Exception as error:
            print("Ошибка:", error)


if __name__ == "__main__":
    asyncio.run(main())
