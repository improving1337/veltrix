import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from telethon import errors

from account_deploy import AccountDeploy


def session_bytes():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'sample.session'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sessions (auth_key BLOB)')
            db.execute('INSERT INTO sessions VALUES (?)', (b'x' * 256,))
        return path.read_bytes()


class FakeClient:
    def __init__(self, path, api_id, api_hash, **kwargs):
        assert api_id == 2040
        assert api_hash == 'b18441a1ff607e10a989891a5462e627'
        Path(path + '.session').write_bytes(session_bytes())
        self.session = NS(close=lambda: None)
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.send_code_request = AsyncMock(return_value=NS(phone_code_hash='private-hash'))
        self.get_me = AsyncMock(return_value=NS(id=1234))
        self.sign_in = AsyncMock()


class DeployTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ['Account', 'Session', 'temp']:
            (self.root / name).mkdir()
        self.accounts = {}
        self.now = 100
        self.connect = AsyncMock(return_value=(1, 1))
        self.deploy = AccountDeploy(self.root, lambda: self.accounts, self.connect,
                                    client_factory=FakeClient, clock=lambda: self.now)
        app = web.Application()
        self.deploy.register(app)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()

    async def asyncTearDown(self):
        await self.deploy.shutdown()
        await self.http.close()
        self.temp.cleanup()

    async def post(self, path, data):
        response = await self.http.post('/api/deploy/' + path, json=data)
        return response, await response.json()

    async def login(self):
        response, body = await self.post('phone', {'phone': '+79991234567'})
        self.assertEqual(response.status, 200)
        self.assertNotIn('private-hash', json.dumps(body))
        return body['token']

    async def test_phone_code_login_persists_public_api(self):
        token = await self.login()
        response, body = await self.post('verify', {'token': token, 'code': '12345'})
        self.assertEqual(response.status, 200)
        self.assertEqual(body['stage'], 'done')
        config = json.loads((self.root / 'Account/phone_1234.json').read_text())
        self.assertEqual(config['app_id'], 2040)
        self.assertTrue((self.root / 'Session/phone_1234.session').exists())
        self.assertEqual(self.deploy.pending, {})
        self.connect.assert_awaited_once()

    async def test_existing_2fa_step_and_invalid_password_retry(self):
        token = await self.login()
        client = self.deploy.pending[token]['client']
        client.sign_in.side_effect = errors.SessionPasswordNeededError(None)
        response, body = await self.post('verify', {'token': token, 'code': '12345'})
        self.assertEqual(body, {'stage': 'password'})
        client.sign_in.side_effect = errors.PasswordHashInvalidError(None)
        response, body = await self.post('verify', {'token': token, 'password': 'wrong'})
        self.assertEqual(response.status, 400)
        client.sign_in.side_effect = None
        response, body = await self.post('verify', {'token': token, 'password': 'secret'})
        self.assertEqual(body['stage'], 'done')
        self.assertNotIn('secret', json.dumps(body))

    async def test_bad_code_retains_challenge(self):
        token = await self.login()
        self.deploy.pending[token]['client'].sign_in.side_effect = errors.PhoneCodeInvalidError(None)
        response, body = await self.post('verify', {'token': token, 'code': '12345'})
        self.assertEqual(response.status, 400)
        self.assertIn(token, self.deploy.pending)

    async def test_expiration_closes_client_and_deletes_temp(self):
        token = await self.login()
        item = self.deploy.pending[token]
        self.now += 601
        response, body = await self.post('verify', {'token': token, 'code': '12345'})
        self.assertEqual(response.status, 410)
        item['client'].disconnect.assert_awaited_once()
        self.assertFalse(item['directory'].exists())

    async def test_duplicate_identity_does_not_replace_files(self):
        token = await self.login()
        self.accounts['existing'] = NS(me=NS(id=1234))
        response, body = await self.post('verify', {'token': token, 'code': '12345'})
        self.assertEqual(response.status, 409)
        self.assertEqual(list((self.root / 'Session').iterdir()), [])

    async def test_phone_normalizes_spaces_parentheses_and_optional_plus(self):
        for value in ('1 234567', '1 (234) 567', '+1\u00a0(234) 567'):
            with self.subTest(value=value):
                response, body = await self.post('phone', {'phone': value})
                self.assertEqual(response.status, 200)
                pending = self.deploy.pending[body['token']]
                pending['client'].send_code_request.assert_awaited_once_with('+1234567')
                duplicate, _ = await self.post('phone', {'phone': '+1234567'})
                self.assertEqual(duplicate.status, 409)
                await self.post('cancel', {'token': body['token']})

    async def test_invalid_phone_and_cancel(self):
        response, body = await self.post('phone', {'phone': '../../bad'})
        self.assertEqual(response.status, 400)
        token = await self.login()
        response, body = await self.post('cancel', {'token': token})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.deploy.pending)

    async def upload(self, filename, content):
        form = FormData()
        form.add_field('file', content, filename=filename, content_type='application/octet-stream')
        response = await self.http.post('/api/deploy/session', data=form)
        return response, await response.json()

    async def test_session_upload_and_duplicate_rejected(self):
        response, body = await self.upload('sample.session', session_bytes())
        self.assertEqual(response.status, 200, body)
        config = json.loads((self.root / 'Account/sample.json').read_text())
        self.assertEqual(config['app_id'], 2040)
        response, body = await self.upload('sample.session', session_bytes())
        self.assertEqual(response.status, 409)

    async def test_invalid_session_not_imported(self):
        response, body = await self.upload('bad.session', b'not sqlite')
        self.assertEqual(response.status, 400)
        self.assertEqual(list((self.root / 'Session').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
