
import asyncio
import csv
import os

from telethon import TelegramClient
from telethon.errors import FloodWaitError

API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
SESSION = "6283865292100"
CHAT = input("Ссылка или username группы: ").strip()
OUTPUT = "members.csv"

async def download_avatar(client, user, folder="export/avatars"):
    os.makedirs(folder, exist_ok=True)

    if not user.photo:
        return ""

    path = os.path.join(folder, f"{user.id}.jpg")
    result = await client.download_profile_photo(
        user,
        file=path
    )

    return result or ""

async def main():
    if not API_ID or not API_HASH:
        print("Укажи TG_API_ID и TG_API_HASH в переменных окружения.")
        return

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        entity = await client.get_entity(CHAT)

        with open(
            OUTPUT, "w", newline="", encoding="utf-8-sig"
        ) as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "id",
                    "username",
                    "first_name",
                    "last_name",
                    "is_bot",
                ],
            )
            writer.writeheader()

            count = 0
            try:
                async for user in client.iter_participants(entity):
                    avatar_path = await download_avatar(client, user)

                    writer.writerow({
                        "id": user.id,
                        "username": user.username or "",
                        "first_name": user.first_name or "",
                        "last_name": user.last_name or "",
                        "is_bot": user.bot,
                    })
                    count += 1
            except FloodWaitError as error:
                print(
                    f"Telegram ограничил запросы на {error.seconds} сек. "
                    "Повтори экспорт позже."
                )
                return

        print(f"Готово: {count} участников.")
        print(f"Файл: {os.path.abspath(OUTPUT)}")


if __name__ == "__main__":
    asyncio.run(main())
