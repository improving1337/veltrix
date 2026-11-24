import tempfile
import unittest
import smtplib
import socket
import json
import stat
from base64 import b64encode, urlsafe_b64decode
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import parse_qs
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telethon import functions, types

import main
import vpn
import gmail_delivery
import gmail_oauth_setup

URL = 'https://zestful.bond/sub/id?key=A%2Fb%3D&trial=1'


class Telegram:
    def __init__(self, ready_after=3):
        self.commands = []
        self.clicks = []
        self.stage = 0
        self.checks = 0
        self.ready_after = ready_after

    def is_connected(self):
        return True

    async def __call__(self, request):
        self.commands.append(request)

    async def get_entity(self, name):
        return NS(bot=True, username=name)

    async def send_message(self, bot, text):
        self.commands.append(text)
        self.stage = 1
        return NS(id=20)

    def button(self, text, next_stage):
        async def click():
            self.clicks.append(text)
            self.stage = next_stage
        return NS(text=text, button=types.KeyboardButton(text, types.InlineButtonTypeCallback(b'btn')),
                  data=b'btn', url=None, click=click)

    async def get_messages(self, peer, limit):
        stale = NS(id=10, raw_text='https://zestful.bond/old', out=False, buttons=None, entities=[])
        if self.stage == 1:
            message = NS(id=21, raw_text='Меню', out=False, buttons=[[self.button('⭐ Ваша подписка', 2)]])
        elif self.stage == 2:
            message = NS(id=21, raw_text='Подписки', out=False,
                         buttons=[[self.button('Другой пункт', 9), self.button('Получить пробный доступ', 3)]])
        elif self.stage == 3:
            message = NS(id=22, raw_text='Подтвердите', out=False, buttons=[[self.button('Получить доступ', 4)]])
        else:
            self.checks += 1
            if self.checks < self.ready_after:
                return [stale]
            message = NS(id=23, raw_text='Подписка готова, ссылка: ' + URL, out=False, buttons=None, entities=[])
        return [message, stale]


class VpnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 200 * vpn.DAYS
        self.telegram = Telegram()
        self.account = NS(me=types.User(123, username='example'), client=self.telegram, name='example.json')
        self.accounts = {'example': self.account}
        self.sleep = AsyncMock()
        self.mail_args = []

    def make_service(self):
        def mail(*args):
            self.mail_args.append(args)
        return vpn.VpnSubscriptions(self.root, lambda: self.accounts, lambda: self.now, self.sleep, mail)

    async def test_issue_complete_link_once_and_mail_through_authenticated_routes(self):
        service = self.make_service()
        app = web.Application(middlewares=[main.auth_mw])
        service.register(app)
        headers = {'Authorization': 'Basic ' + b64encode(f'{main.PANEL_USER}:test'.encode()).decode(), 'X-Panel': '1'}
        with patch.object(main, 'PANEL_PASSWORD', 'test'):
            async with TestClient(TestServer(app)) as http:
                self.assertEqual((await http.get('/api/vpn')).status, 401)
                response = await http.post('/api/vpn/issue', headers=headers)
                self.assertEqual(response.status, 200)
                result = await response.json()
                self.assertEqual(result['url'], URL)
                self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [1, 5, 5, 5])
                self.assertIsInstance(self.telegram.commands[0], functions.channels.JoinChannelRequest)
                self.assertEqual(self.telegram.commands[1], '/start')
                self.assertEqual(self.telegram.clicks, ['⭐ Ваша подписка', 'Получить пробный доступ', 'Получить доступ'])
                self.assertEqual((await http.post('/api/vpn/issue', headers=headers)).status, 400)
                response = await http.post('/api/vpn/email', headers=headers, json={
                    'recipient': 'user+vpn@example.org', 'receipt': result['receipt'], 'url': 'https://example.org'})
                self.assertEqual(response.status, 200)
                self.assertEqual(self.mail_args, [(self.root, 'user+vpn@example.org', URL)])
                snapshot = await (await http.get('/api/vpn', headers=headers)).json()
                self.assertNotIn(result['receipt'], str(snapshot))
                self.assertNotIn(URL, str(snapshot))
                self.assertEqual(snapshot['records'][0]['remaining'], 31 * vpn.DAYS)

    async def test_pending_link_can_be_recovered_after_restart_without_repeating_click(self):
        self.telegram.ready_after = 4
        with self.assertRaisesRegex(ValueError, 'пока не прислал'):
            await self.make_service().issue()
        restored = self.make_service()
        self.assertEqual(restored.snapshot()['records'][0]['status'], 'pending')
        self.assertEqual((await restored.issue())['url'], URL)
        self.assertEqual(self.telegram.clicks.count('Получить доступ'), 1)
        self.assertEqual(self.telegram.checks, 4)

    async def test_gmail_settings_take_effect_without_restart_and_redact_credentials(self):
        service = vpn.VpnSubscriptions(self.root, lambda: self.accounts, lambda: self.now, self.sleep)
        service.put('123', {'receipt': 'vpn-id', 'url': URL, 'issued_at': self.now})
        app = web.Application(middlewares=[main.auth_mw])
        service.register(app)
        headers = {'Authorization': 'Basic ' + b64encode(f'{main.PANEL_USER}:test'.encode()).decode(), 'X-Panel': '1'}
        endpoint = '/api/vpn/gmail-settings'
        settings = {'transport': 'api', 'email': 'sender@gmail.com', 'client_id': 'test-client-id',
                    'client_secret': 'test-client-secret', 'refresh_token': 'test-refresh-token'}
        with (patch.object(main, 'PANEL_PASSWORD', 'test'), patch('vpn.send_gmail_api') as send,
              patch('vpn.send_gmail') as smtp):
            async with TestClient(TestServer(app)) as http:
                self.assertEqual((await http.get(endpoint)).status, 401)
                self.assertEqual((await http.post(endpoint, json=settings)).status, 401)
                old = await http.post(endpoint, headers=headers, json={'transport': 'smtp'})
                self.assertEqual(old.status, 200)
                payload = {'recipient': 'target@example.com', 'receipt': 'vpn-id'}
                self.assertEqual((await http.post('/api/vpn/email', headers=headers, json=payload)).status, 200)
                smtp.assert_called_once()
                response = await http.post(endpoint, headers=headers, json=settings)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertEqual((await http.post(endpoint, headers=headers, json={'refresh_token': ''})).status, 200)
                snapshot = await (await http.get(endpoint, headers=headers)).json()
                for key in ('client_id', 'client_secret', 'refresh_token'):
                    self.assertTrue(snapshot['configured'][key])
                    self.assertNotIn(settings[key], str(snapshot))
                self.assertEqual((await http.post('/api/vpn/email', headers=headers, json=payload)).status, 200)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]['refresh_token'], settings['refresh_token'])
        self.assertEqual(send.call_args.args[1].get_content().strip(), URL)
        restored = gmail_delivery.GmailSettings(self.root)
        self.assertEqual(restored.snapshot(), snapshot)
        self.assertEqual(stat.S_IMODE(restored.path.stat().st_mode), 0o600)

    def test_gmail_api_refreshes_token_and_sends_complete_link(self):
        settings = {'client_id': 'id', 'client_secret': 'secret', 'refresh_token': 'refresh'}
        message = vpn.gmail_message('from@gmail.com', 'target@example.com', URL)
        with patch('gmail_delivery.post_google', side_effect=[{'access_token': 'access'}, {'id': 'sent'}]) as post:
            gmail_delivery.send_gmail_api(settings, message)
        refresh, send = [call.args for call in post.call_args_list]
        self.assertEqual(refresh[0], gmail_delivery.TOKEN_URL)
        self.assertEqual(parse_qs(refresh[1].decode()), {
            'client_id': ['id'], 'client_secret': ['secret'], 'refresh_token': ['refresh'],
            'grant_type': ['refresh_token']})
        self.assertEqual(send[0], gmail_delivery.SEND_URL)
        self.assertEqual(send[2]['Authorization'], 'Bearer access')
        delivered = BytesParser(policy=policy.default).parsebytes(urlsafe_b64decode(json.loads(send[1])['raw']))
        self.assertEqual(delivered['To'], 'target@example.com')
        self.assertEqual(delivered.get_content().strip(), URL)

    def test_oauth_helper_stores_credentials_privately_without_overwriting(self):
        path = self.root / 'gmail-credentials.env'
        client = {'client_id': 'fake-id', 'client_secret': 'fake-secret'}
        gmail_oauth_setup.write_settings(path, client, 'fake-refresh-token')
        settings = gmail_delivery.GmailSettings(self.root)
        (self.root / '.env').write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(settings.effective()['refresh_token'], 'fake-refresh-token')
            self.assertEqual(settings.effective()['transport'], 'api')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            gmail_oauth_setup.write_settings(path, client, 'another-token')

    async def test_record_age_boundary_and_exclusion_are_persisted(self):
        self.telegram.ready_after = 1
        service = self.make_service()
        await service.issue()
        service.edit('123', self.now - 26 * vpn.DAYS)
        self.assertEqual(service.snapshot()['records'][0]['status'], 'active')
        self.now += 60
        self.assertEqual(service.snapshot()['records'][0]['status'], 'ending')
        self.now += 5 * vpn.DAYS
        self.assertEqual(service.snapshot()['records'][0]['status'], 'expired')
        service.edit('123', exclude=True)
        restored = self.make_service()
        self.assertEqual(restored.snapshot()['records'], [])
        with self.assertRaisesRegex(ValueError, 'Нет подключённых аккаунтов'):
            await restored.issue()

    def test_links_exact_host_and_hidden_entities(self):
        entity = types.MessageEntityTextUrl(0, 7, URL)
        message = NS(raw_text='https://zestful.bond.evil.net/something https://zestful.bond@evil.net/path',
                     entities=[entity], buttons=None)
        self.assertEqual(vpn.match_link(message, 'zestful.bond'), URL)
        message.entities = []
        self.assertIsNone(vpn.match_link(message, 'zestful.bond'))
        precise = NS(raw_text='🔑 ' + URL + '.', buttons=None,
                     entities=[types.MessageEntityUrl(3, len(URL))])
        self.assertEqual(vpn.match_link(precise, 'zestful.bond'), URL)

    def test_gmail_uses_tls_and_configured_credentials(self):
        with (patch.dict('os.environ', {'GMAIL_EMAIL': 'sender@gmail.com', 'GMAIL_APP_PASSWORD': 'one two',
                                       'GMAIL_SMTP_PORT': '465'}),
              patch('vpn.smtplib.SMTP_SSL') as connect):
            server = connect.return_value.__enter__.return_value
            vpn.send_gmail(self.root, 'receiver@example.com', URL)
        self.assertEqual(connect.call_args.args, ('smtp.gmail.com', 465))
        self.assertTrue(connect.call_args.kwargs['context'].check_hostname)
        server.login.assert_called_once_with('sender@gmail.com', 'onetwo')
        message = server.send_message.call_args.args[0]
        self.assertEqual(str(message['To']), 'receiver@example.com')
        self.assertEqual(message.get_content().strip(), URL)
        for recipient in ('user@example.com\r\nBcc: other@example.com', 'one@example.com,two@example.com'):
            with self.assertRaises(ValueError):
                vpn.recipient_address(recipient)

    def test_gmail_port_587_enables_tls_before_login(self):
        with (patch.dict('os.environ', {'GMAIL_EMAIL': 'sender@gmail.com', 'GMAIL_APP_PASSWORD': 'test',
                                       'GMAIL_SMTP_PORT': '587'}),
              patch('vpn.smtplib.SMTP') as connect):
            server = connect.return_value.__enter__.return_value
            vpn.send_gmail(self.root, 'receiver@example.com', URL)
        connect.assert_called_once_with('smtp.gmail.com', 587, timeout=20)
        self.assertEqual([call[0] for call in server.method_calls], ['starttls', 'login', 'send_message'])
        self.assertTrue(server.starttls.call_args.kwargs['context'].check_hostname)

    def test_gmail_diagnostics_distinguish_dns_connection_and_rejected_delivery(self):
        with patch.dict('os.environ', {'GMAIL_EMAIL': 'sender@gmail.com', 'GMAIL_APP_PASSWORD': 'test',
                                      'GMAIL_SMTP_PORT': '465'}):
            for error, pattern in [(socket.gaierror(-3, 'temporary'), 'разрешить имя smtp.gmail.com'),
                                   (TimeoutError(), 'Нет соединения с smtp.gmail.com:465')]:
                with (patch('vpn.smtplib.SMTP_SSL', side_effect=error),
                      self.assertRaisesRegex(ValueError, pattern)):
                    vpn.send_gmail(self.root, 'receiver@example.com', URL)
            with patch('vpn.smtplib.SMTP_SSL') as connect:
                server = connect.return_value.__enter__.return_value
                server.send_message.side_effect = smtplib.SMTPDataError(554, b'raw response')
                with self.assertRaisesRegex(ValueError, r'Gmail вернул ошибку SMTP 554\.'):
                    vpn.send_gmail(self.root, 'receiver@example.com', URL)
                server.send_message.side_effect = None
                connect.return_value.__exit__.side_effect = smtplib.SMTPServerDisconnected()
                vpn.send_gmail(self.root, 'receiver@example.com', URL)
