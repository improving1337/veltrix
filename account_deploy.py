"""Phone/code/password login and individual .session imports for the panel."""
import asyncio
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient, errors

from account_security import private_json


def fail(message, status=400):
    return web.json_response({"error": message}, status=status)


def publish_session(source, destination):
    """Publish a complete 0600 file atomically without replacing an existing session."""
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".session-")
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AccountDeploy:
    def __init__(self, directory, accounts, connect_new, client_factory=TelegramClient, clock=time.time):
        self.directory = Path(directory)
        self.accounts = accounts
        self.connect_new = connect_new
        self.client_factory = client_factory
        self.clock = clock
        self.on_created = None
        self.pending = {}
        self.lock = asyncio.Lock()

    async def request(self, operation):
        return await asyncio.wait_for(operation, 45)

    async def discard(self, token):
        item = self.pending.pop(token, None)
        if item:
            try:
                await asyncio.wait_for(item["client"].disconnect(), 10)
            finally:
                item["client"].session.close()
                shutil.rmtree(item["directory"], ignore_errors=True)

    async def cleanup(self):
        async with self.lock:
            for token, item in list(self.pending.items()):
                if item["expires"] <= self.clock():
                    try:
                        await self.discard(token)
                    except Exception:
                        pass

    async def shutdown(self):
        for token in list(self.pending):
            try:
                await self.discard(token)
            except Exception:
                pass

    async def send_code(self, request):
        data = await request.json()
        phone = str(data.get("phone", "")).strip()
        if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            return fail("Введите номер в международном формате: +79991234567")
        async with self.lock:
            if len(self.pending) >= 20:
                return fail("Слишком много незавершённых входов. Отмените их или подождите 10 минут.", 429)
            if any(item["phone"] == phone for item in self.pending.values()):
                return fail("Для этого номера уже запрошен код. Завершите или отмените вход.", 409)
            if any(str(getattr(a.me, "phone", "")).lstrip("+") == phone[1:] for a in self.accounts().values()):
                return fail("Этот номер уже добавлен в панель.", 409)
            temporary = Path(tempfile.mkdtemp(prefix="login-", dir=self.directory / "temp"))
            token = secrets.token_urlsafe(32)
            try:
                client = self.client_factory(str(temporary / "account"), 2040, "b18441a1ff607e10a989891a5462e627",
                                             flood_sleep_threshold=0)
            except Exception as exc:
                shutil.rmtree(temporary, ignore_errors=True)
                return self.error(exc)
            self.pending[token] = {"client": client, "directory": temporary, "phone": phone,
                                   "expires": self.clock() + 600, "stage": "code"}
            try:
                await self.request(client.connect())
                sent = await self.request(client.send_code_request(phone))
                self.pending[token]["hash"] = sent.phone_code_hash
            except Exception as exc:
                await self.discard(token)
                return self.error(exc)
            return web.json_response({"token": token, "stage": "code", "expires_in": 600})

    def error(self, exc):
        if isinstance(exc, errors.FloodWaitError):
            return fail(f"Telegram просит подождать {exc.seconds} секунд.", 429)
        messages = {
            "PhoneCodeInvalidError": "Неверный код. Попробуйте ещё раз.",
            "PhoneCodeExpiredError": "Код истёк. Отмените вход и запросите новый.",
            "PasswordHashInvalidError": "Неверный пароль 2FA.",
            "PhoneNumberInvalidError": "Неверный номер телефона.",
            "PhoneNumberBannedError": "Номер заблокирован Telegram.",
            "AuthRestartError": "Telegram требует повторить вход. Запросите новый код.",
            "TimeoutError": "Telegram не ответил вовремя. Попробуйте ещё раз.",
        }
        return fail(messages.get(type(exc).__name__, f"Ошибка Telegram: {type(exc).__name__}"))

    async def verify(self, request):
        data = await request.json()
        token = str(data.get("token", ""))
        async with self.lock:
            item = self.pending.get(token)
            if not item:
                return fail("Вход не найден. Запросите код снова.", 404)
            if self.clock() >= item["expires"]:
                await self.discard(token)
                return fail("Время входа истекло. Запросите код снова.", 410)
            client = item["client"]
            try:
                if item["stage"] == "password":
                    password = data.get("password")
                    if not isinstance(password, str) or not password:
                        return fail("Введите текущий пароль 2FA.")
                    await self.request(client.sign_in(password=password))
                elif item["stage"] == "code":
                    code = str(data.get("code", "")).replace(" ", "").strip()
                    if not re.fullmatch(r"\d{4,8}", code):
                        return fail("Введите код из Telegram.")
                    await self.request(client.sign_in(phone=item["phone"], code=code, phone_code_hash=item["hash"]))
                return await self.finish(token)
            except errors.SessionPasswordNeededError:
                item["stage"] = "password"
                return web.json_response({"stage": "password"})
            except Exception as exc:
                return self.error(exc)

    async def finish(self, token):
        item = self.pending[token]
        client = item["client"]
        me = item.get("me") or await self.request(client.get_me())
        if me is None:
            return fail("Telegram не подтвердил авторизацию.")
        if any(getattr(a.me, "id", None) == me.id for a in self.accounts().values()):
            await self.discard(token)
            return fail("Этот Telegram-аккаунт уже добавлен в панель.", 409)
        name = "phone_" + str(me.id)
        session = self.directory / "Session" / (name + ".session")
        config = self.directory / "Account" / (name + ".json")
        if session.exists() or config.exists():
            await self.discard(token)
            return fail("Файлы этого аккаунта уже существуют. Они не были перезаписаны.", 409)
        # Mark as authorized so a failed disk operation can be retried without using the code again.
        item["stage"] = "authorized"
        item["me"] = me
        await self.request(client.disconnect())
        client.session.close()
        private_json(config, {"app_id": 2040, "app_hash": "b18441a1ff607e10a989891a5462e627", "phone": item["phone"]}, exclusive=True)
        try:
            publish_session(item["directory"] / "account.session", session)
        except Exception:
            config.unlink(missing_ok=True)
            raise
        self.pending.pop(token)
        shutil.rmtree(item["directory"], ignore_errors=True)
        added, connected = await self.connect_new()
        if self.on_created:
            self.on_created(name)
        return web.json_response({"stage": "done", "account": name, "added": added, "connected": connected})

    async def cancel(self, request):
        data = await request.json()
        async with self.lock:
            await self.discard(str(data.get("token", "")))
        return web.json_response({"ok": True})

    async def upload(self, request):
        async with self.lock:
            reader = await request.multipart()
            part = await reader.next()
            if part is None or not part.filename:
                return fail("Выберите файл .session.")
            filename = part.filename
            if not re.fullmatch(r"[A-Za-z0-9_+\-]{1,100}\.session", filename):
                return fail("Имя файла: латинские буквы, цифры, +, - и _, расширение .session.")
            name = Path(filename).stem
            session = self.directory / "Session" / filename
            config = self.directory / "Account" / (name + ".json")
            if session.exists() or config.exists() or name in self.accounts():
                return fail("Аккаунт или файл с таким именем уже существует.", 409)
            temporary = Path(tempfile.mkdtemp(prefix="session-", dir=self.directory / "temp"))
            source = temporary / filename
            try:
                total = 0
                with source.open("xb") as stream:
                    while chunk := await part.read_chunk():
                        total += len(chunk)
                        if total > 15 * 1024 * 1024:
                            return fail("Файл .session больше 15 МБ.", 413)
                        stream.write(chunk)
                try:
                    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
                        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValueError()
                        row = db.execute("SELECT auth_key FROM sessions").fetchone()
                        if not row or not row[0]:
                            raise ValueError()
                except (sqlite3.Error, ValueError):
                    return fail("Это не авторизованный файл сессии Telethon (SQLite).")
                private_json(config, {"app_id": 2040, "app_hash": "b18441a1ff607e10a989891a5462e627"}, exclusive=True)
                try:
                    publish_session(source, session)
                except Exception:
                    config.unlink(missing_ok=True)
                    raise
                added, connected = await self.connect_new()
                return web.json_response({"account": name, "added": added, "connected": connected})
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    def register(self, app):
        app.router.add_post("/api/deploy/phone", self.send_code)
        app.router.add_post("/api/deploy/verify", self.verify)
        app.router.add_post("/api/deploy/cancel", self.cancel)
        app.router.add_post("/api/deploy/session", self.upload)
