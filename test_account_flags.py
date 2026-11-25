import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import account_flags
import channels
import main
import panel_settings


class AccountFlagTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.flags = account_flags.AccountFlags(self.root)
        self.prefs = panel_settings.PanelSettings(self.root)

    async def test_deploy_default_loaded_and_old_labels_unchanged(self):
        folders = {part: self.root / part for part in ('Account', 'Session')}
        for folder in folders.values():
            folder.mkdir()
        accounts = {}
        self.addCleanup(accounts.clear)

        def add_session(name):
            (folders['Session'] / (name + '.session')).touch()
            return main.load_accounts()

        app = web.Application()
        app.router.add_post('/settings', self.prefs.update)
        async with TestClient(TestServer(app)) as client:
            with patch.multiple(main, ACCOUNT_FLAGS=self.flags, PANEL_PREFERENCES=self.prefs, ACCOUNTS=accounts,
                                ACCOUNT_DIR=folders['Account'], SESSION_DIR=folders['Session'],
                                ACCOUNT_LOAD_ERRORS={}, TelegramClient=lambda *args, **kwargs: object()):
                self.assertEqual([acc.name for acc in add_session('existing')], ['existing'])
                response = await client.post('/settings', json={'autopilot_for_new_accounts': True})
                self.assertEqual(response.status, 200)
                self.assertEqual([acc.name for acc in add_session('new')], ['new'])
                response = await client.post('/settings', json={'autopilot_for_new_accounts': False})
                self.assertEqual(response.status, 200)
                self.assertEqual([acc.name for acc in add_session('another')], ['another'])
                for account in accounts.values():
                    account.me = object()
                self.assertEqual(set(main.service_clients()), {'existing', 'new', 'another'})
                self.assertEqual(set(main.service_clients(general_only=True)), {'existing', 'another'})
        restored = account_flags.AccountFlags(self.root)
        self.assertEqual(restored.get('new'), 'autopilot')
        self.assertEqual(restored.get('existing'), 'general')

    async def test_flag_applies_to_aliases_and_reissued_session(self):
        self.flags.register(['original', 'alias'])
        self.flags.bind('original', 15)
        self.flags.bind('alias', 15)
        app = web.Application(middlewares=[main.auth_mw, main.error_mw])
        app.router.add_post('/api/accounts/{acc}/flag', main.api_account_flag)
        credentials = base64.b64encode(f'{main.PANEL_USER}:test'.encode()).decode()
        headers = {'Authorization': 'Basic ' + credentials, 'X-Panel': '1'}
        accounts = {name: SimpleNamespace() for name in ('original', 'alias')}
        with patch.multiple(main, ACCOUNT_FLAGS=self.flags, ACCOUNTS=accounts, PANEL_PASSWORD='test'):
            async with TestClient(TestServer(app)) as client:
                path = '/api/accounts/original/flag'
                self.assertEqual((await client.post(path, json={'flag': 'autopilot'})).status, 401)
                response = await client.post(path, json={'flag': 'autopilot'}, headers=headers)
                self.assertEqual(await response.json(), {'flags': {'original': 'autopilot', 'alias': 'autopilot'}})
                self.assertEqual((await client.post(path, json={'flag': 'other'}, headers=headers)).status, 400)
        restored = account_flags.AccountFlags(self.root)
        restored.register(['reissued'])
        restored.bind('reissued', 15)
        restored.forget('original')
        self.assertEqual(restored.get('reissued'), 'autopilot')
        self.assertEqual(restored.get('alias'), 'autopilot')
        restored.register(['offline'])
        restored.select('offline', 'general')
        restored.bind('offline', 15)
        self.assertEqual(restored.get('reissued'), 'general')

    async def test_stale_order_and_pending_step_cannot_use_autopilot_account(self):
        self.flags.register(['reserved'], 'autopilot')
        joiner = channels.ChannelJoiner(lambda: {})
        joiner.account_allowed = lambda name: self.flags.get(name) == 'general'
        joiner.plans['plan'] = {'items': [{'account': 'reserved'}]}
        with self.assertRaisesRegex(ValueError, 'Метки аккаунтов изменились'):
            await joiner.start('plan')
        job = {'stop': asyncio.Event(), 'resumed': True}
        item = {'account': 'reserved'}
        self.assertIsNone(await joiner._ready_client(job, item))
        self.assertIn('Автопилота', item['note'])
        self.assertTrue(await joiner._action_extra(job, item)['wait_before_action']())
