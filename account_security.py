"""Persistent account protection. Never expose passwords through HTTP or logs."""
import asyncio
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from aiohttp import web
from telethon import errors, functions


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def private_json(path, data, *, exclusive=False):
    """Atomic, private write; keep the previous file intact on failure."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AccountSecurity:
    def __init__(self, directory, accounts, clock=time.time):
        self.directory = Path(directory) / "security"
        self.state_path = self.directory / "state.json"
        self.password_path = self.directory / "passwords.json"
        self.pending_path = self.directory / "pending_passwords.json"
        self.state = read_json(self.state_path)
        self.passwords = read_json(self.directory / "password_index.json")
        self.pending = read_json(self.pending_path)
        self.suspended = set()
        self.accounts = accounts
        self.clock = clock
        self.lock = asyncio.Lock()

    def save(self):
        private_json(self.state_path, self.state)

    def info(self, name):
        record = self.state.get("accounts", {}).get(name, {})
        # Explicit allowlist: no password ever leaves this module.
        return {key: record.get(key) for key in (
            "two_factor_enabled", "two_factor_status", "session_count", "session_status",
            "next_sessions_check", "next_2fa_check", "checked_at", "error")}

    async def request(self, operation):
        return await asyncio.wait_for(operation, 45)

    async def check(self, account):
        """Serial execution prevents conflicting password writes and session resets."""
        async with self.lock:
            await self._check(account)

    async def _check(self, account):
        if account.name in self.suspended or account.me is None or self.accounts().get(account.name) is not account:
            return
        record = self.state.setdefault("accounts", {}).setdefault(account.name, {})
        identity = str(account.me.id)
        if record.get("telegram_id") not in (None, identity):
            record.clear()  # A different user was imported with the same filename.
        record["telegram_id"] = identity
        # Do not revoke another session which this panel is also using.
        if sum(getattr(a.me, "id", None) == account.me.id for a in self.accounts().values()) > 1:
            record["error"] = "Этот Telegram-аккаунт добавлен несколько раз; оставьте одну сессию в панели."
            self.save()
            return
        now = self.clock()
        if now < record.get("next_2fa_check", 0) and now < record.get("next_sessions_check", 0):
            return
        record["error"] = None
        if now >= record.get("next_2fa_check", 0):
            try:
                await self.ensure_2fa(account, record, identity)
                record["next_2fa_check"] = now + 86400
            except errors.FloodWaitError as exc:
                record["next_2fa_check"] = now + max(86400, exc.seconds)
                record["two_factor_status"] = "Ожидание ограничения Telegram"
            except Exception as exc:
                # A timeout may mean Telegram accepted the password. Keep the candidate.
                record["next_2fa_check"] = now + 86400
                record["two_factor_status"] = "Не подтверждено; повтор через 24 часа"
                record["error"] = type(exc).__name__
            self.save()
        # Password errors do not prevent an independent session check.
        if now >= record.get("next_sessions_check", 0):
            try:
                auths = (await self.request(account.client(functions.account.GetAuthorizationsRequest()))).authorizations
                record["session_count"] = len(auths)
                if not any(a.current for a in auths):
                    raise RuntimeError("CurrentSessionMissing")
                if len(auths) > 1:
                    record["session_status"] = "Проверяется завершение других сессий"
                    self.save()
                    confirmed = await self.request(account.client(functions.auth.ResetAuthorizationsRequest()))
                    if not confirmed:
                        raise RuntimeError("SessionResetUnconfirmed")
                    auths = (await self.request(account.client(functions.account.GetAuthorizationsRequest()))).authorizations
                    record["session_count"] = len(auths)
                if len(auths) != 1 or not auths[0].current:
                    raise RuntimeError("OnlyCurrentSessionNotConfirmed")
                record["session_status"] = "Осталась только текущая сессия"
                record["next_sessions_check"] = now + 86400
            except errors.FloodWaitError as exc:
                record["session_status"] = "Ожидание ограничения Telegram"
                record["next_sessions_check"] = now + max(86400, exc.seconds)
            except errors.FreshResetAuthorisationForbiddenError:
                record["session_status"] = "Новая сессия: повтор через 24 часа"
                record["next_sessions_check"] = now + 86400
            except Exception as exc:
                record["session_status"] = "Не подтверждено; повтор через 24 часа"
                record["next_sessions_check"] = now + 86400
                record["error"] = type(exc).__name__
        record["checked_at"] = self.clock()
        self.save()

    async def ensure_2fa(self, account, record, identity):
        password_info = await self.request(account.client(functions.account.GetPasswordRequest()))
        record["two_factor_enabled"] = bool(password_info.has_password)
        if password_info.has_password:
            if identity in self.pending:
                # Verify an indeterminate previous operation without changing an existing password.
                from telethon.password import compute_check
                check = compute_check(password_info, self.pending[identity]["password"])
                await self.request(account.client(functions.auth.CheckPasswordRequest(check)))
                self.commit_password(identity)
            record["two_factor_status"] = "2FA включена; пароль сохранён" if identity in self.passwords else "2FA уже включена; пароль не изменён"
            return
        # Persist BEFORE the request so crashes/timeouts cannot lose the password.
        candidate = self.pending.setdefault(identity, {"account": account.name, "password": secrets.token_urlsafe(24)})
        private_json(self.pending_path, self.pending)
        changed = await self.request(account.client.edit_2fa(new_password=candidate["password"], hint="Account security"))
        if not changed:
            raise RuntimeError("PasswordChangeUnconfirmed")
        verified = await self.request(account.client(functions.account.GetPasswordRequest()))
        if not verified.has_password:
            raise RuntimeError("PasswordEnableUnconfirmed")
        self.commit_password(identity)
        record["two_factor_enabled"] = True
        record["two_factor_status"] = "2FA включена; пароль сохранён"

    def commit_password(self, identity):
        candidate = self.pending[identity]
        # Publicly documented format is {"account": "password"}; file stays on hosting.
        updated = dict(self.passwords)
        updated[identity] = dict(candidate)
        private_json(self.password_path, {item["account"]: item["password"] for item in updated.values()})
        # Identity index allows correct handling of renamed session files after restart.
        private_json(self.directory / "password_index.json", updated)
        self.passwords = updated
        self.pending.pop(identity, None)
        private_json(self.pending_path, self.pending)

    async def run(self):
        while True:
            if self.state.get("enabled", True):
                for account in list(self.accounts().values()):
                    try:
                        await self.check(account)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Never log exception text: SDK errors might contain sensitive data.
                        print(f"[SECURITY] Storage/check error: {type(exc).__name__}", flush=True)
            await asyncio.sleep(30)

    def register(self, app):
        async def status(request):
            return web.json_response({"enabled": self.state.get("enabled", True),
                                      "items": {name: self.info(name) for name in self.accounts()},
                                      "password_file": "security/passwords.json"})

        async def configure(request):
            data = await request.json()
            if type(data.get("enabled")) is not bool:
                raise web.HTTPBadRequest(text='{"error":"enabled должен быть boolean"}', content_type="application/json")
            async with self.lock:
                self.state["enabled"] = data["enabled"]
                self.save()
            return web.json_response({"enabled": self.state["enabled"]})

        app.router.add_get("/api/security", status)
        app.router.add_post("/api/security", configure)
