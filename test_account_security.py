import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from telethon import errors, functions

from account_security import AccountSecurity, private_json


class FakeTelegram:
    def __init__(self, sessions=1, has_password=False, reset_error=None):
        self.sessions = sessions
        self.has_password = has_password
        self.reset_error = reset_error
        self.requests = []
        self.password_changes = []

    async def __call__(self, request):
        self.requests.append(type(request))
        if isinstance(request, functions.account.GetPasswordRequest):
            return NS(has_password=self.has_password)
        if isinstance(request, functions.account.GetAuthorizationsRequest):
            return NS(authorizations=[NS(current=i == 0) for i in range(self.sessions)])
        if isinstance(request, functions.auth.ResetAuthorizationsRequest):
            if self.reset_error:
                raise self.reset_error
            self.sessions = 1
            return True
        raise AssertionError(type(request))

    async def edit_2fa(self, **kwargs):
        self.password_changes.append(kwargs)
        self.has_password = True
        return True


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000
        self.client = FakeTelegram()
        self.account = NS(name="account_1", me=NS(id=123), client=self.client)
        self.accounts = {self.account.name: self.account}
        self.manager = self.reload()

    def reload(self):
        return AccountSecurity(self.root, lambda: self.accounts, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    async def test_enable_2fa_and_single_session_no_reset(self):
        await self.manager.check(self.account)
        self.assertEqual(len(self.client.password_changes), 1)
        passwords = json.loads(self.manager.password_path.read_text())
        self.assertEqual(passwords["account_1"], self.client.password_changes[0]["new_password"])
        self.assertEqual(stat.S_IMODE(self.manager.password_path.stat().st_mode), 0o600)
        self.assertNotIn(functions.auth.ResetAuthorizationsRequest, self.client.requests)
        self.assertNotIn(passwords["account_1"], json.dumps(self.manager.info("account_1")))
        await self.reload().check(self.account)
        self.assertEqual(len(self.client.password_changes), 1)
        self.now += 86400
        await self.reload().check(self.account)
        self.assertEqual(len(self.client.password_changes), 1)

    async def test_multiple_sessions_reset_and_verify(self):
        self.client.sessions = 3
        await self.manager.check(self.account)
        self.assertEqual(self.client.requests.count(functions.account.GetAuthorizationsRequest), 2)
        self.assertEqual(self.manager.info("account_1")["session_count"], 1)

    async def test_fresh_session_wait_persists_restart(self):
        self.client.sessions = 2
        self.client.reset_error = errors.FreshResetAuthorisationForbiddenError(None)
        await self.manager.check(self.account)
        self.assertEqual(self.manager.info("account_1")["next_sessions_check"], 87400)
        restarted = self.reload()
        await restarted.check(self.account)
        self.assertEqual(self.client.requests.count(functions.auth.ResetAuthorizationsRequest), 1)
        self.now += 86400
        self.client.reset_error = None
        await restarted.check(self.account)
        self.assertEqual(self.client.requests.count(functions.auth.ResetAuthorizationsRequest), 2)
        self.assertEqual(restarted.info("account_1")["session_count"], 1)

    async def test_existing_2fa_unchanged(self):
        self.client.has_password = True
        await self.manager.check(self.account)
        self.assertEqual(self.client.password_changes, [])
        self.assertFalse(self.manager.password_path.exists())
        self.assertTrue(self.manager.info("account_1")["two_factor_enabled"])

    async def test_timeout_keeps_candidate_before_change(self):
        async def timeout(**kwargs):
            pending = json.loads(self.manager.pending_path.read_text())
            self.assertEqual(pending['123']['password'], kwargs['new_password'])
            raise asyncio.TimeoutError()
        self.client.edit_2fa = timeout
        await self.manager.check(self.account)
        self.assertIn('123', self.reload().pending)
        self.assertFalse(self.manager.password_path.exists())
        self.assertEqual(self.manager.info("account_1")["session_count"], 1)

    async def test_reset_not_confirmed_does_not_report_success(self):
        original = self.client.__call__
        class Client(FakeTelegram):
            async def __call__(this, request):
                if isinstance(request, functions.auth.ResetAuthorizationsRequest):
                    return False
                return await original(request)
        self.client.sessions = 2
        self.account.client = Client()
        await self.manager.check(self.account)
        info = self.manager.info("account_1")
        self.assertEqual(info["session_count"], 2)
        self.assertEqual(info["error"], "RuntimeError")
        self.assertIn("Не подтверждено", info["session_status"])
        await self.manager.check(self.account)
        self.assertEqual(self.manager.info("account_1")["error"], "RuntimeError")

    async def test_flood_wait_respected(self):
        self.client.sessions = 2
        self.client.reset_error = errors.FloodWaitError(None, capture=100000)
        await self.manager.check(self.account)
        self.assertEqual(self.manager.info("account_1")["next_sessions_check"], 101000)

    async def test_duplicate_user_no_changes(self):
        self.accounts['duplicate'] = NS(me=NS(id=123))
        await self.manager.check(self.account)
        self.assertEqual(self.client.requests, [])

    async def test_zero_sessions_no_reset(self):
        self.client.sessions = 0
        await self.manager.check(self.account)
        self.assertNotIn(functions.auth.ResetAuthorizationsRequest, self.client.requests)
        self.assertIsNotNone(self.manager.info("account_1")["error"])

    async def test_pending_password_recovery_after_indeterminate_change(self):
        private_json(self.manager.pending_path, {'123': {'account': 'account_1', 'password': 'candidate'}})
        restarted = self.reload()
        self.client.has_password = True
        original = self.client.__call__
        class Client(FakeTelegram):
            async def __call__(this, request):
                if isinstance(request, functions.auth.CheckPasswordRequest):
                    return NS()
                return await original(request)
        self.account.client = Client()
        with patch('telethon.password.compute_check', return_value=NS()):
            await restarted.check(self.account)
        self.assertEqual(json.loads(restarted.password_path.read_text()), {'account_1': 'candidate'})
        self.assertEqual(restarted.pending, {})
        self.assertEqual(self.client.password_changes, [])


if __name__ == '__main__':
    unittest.main()
