import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

import channels
from subscriptions import CACHE_SECONDS, Subscriptions


def channel(identity, title, **fields):
    return types.Channel(id=identity, title=title, photo=types.ChatPhotoEmpty(),
                         date=datetime.now(timezone.utc), **fields)


class Client:
    def __init__(self, *entities):
        self.entities = entities
        self.leave = AsyncMock()
        self.scans = 0

    def is_connected(self):
        return True

    async def iter_dialogs(self, **kwargs):
        self.scans += 1
        for entity in self.entities:
            yield SimpleNamespace(entity=entity)

    async def __call__(self, request):
        return await self.leave(request)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.prefs = {"subscription_hours": 72, "subscription_spread": 0}
        self.now = 100000
        public = channel(10, "Новости", broadcast=True, access_hash=100)
        other = channel(11, "Другой", broadcast=True, access_hash=101)
        group = channel(20, "Группа", megagroup=True, access_hash=200)
        left = channel(30, "Покинутый", broadcast=True, left=True, access_hash=300)
        self.clients = [Client(public, other, group, left), Client(public)]
        self.accounts = {}
        for number, client in enumerate(self.clients, 1):
            name = str(number)
            me = types.User(number, first_name="Аккаунт " + name)
            self.accounts[name] = SimpleNamespace(name=name, me=me, client=client, channels=None)
        self.registry = Subscriptions(self.directory, lambda: self.accounts, lambda: self.prefs, lambda: self.now)

    async def scan(self):
        with patch("subscriptions.SCAN_GAP", 0):
            await self.registry.refresh()

    async def test_filters_channels_and_reuses_cache_after_restart(self):
        await self.scan()
        await self.scan()
        data = self.registry.snapshot()
        self.assertEqual([(c["id"], c["count"], c["enabled"]) for c in data["channels"]],
                         [("10", 2, False), ("11", 1, False)])
        self.assertEqual([client.scans for client in self.clients], [1, 1])
        self.assertEqual(self.accounts["1"].channels, 2)
        restored = Subscriptions(self.directory, lambda: self.accounts, lambda: self.prefs, lambda: self.now)
        self.assertEqual(restored.snapshot(), data)
        self.now += CACHE_SECONDS
        await self.scan()
        self.assertEqual([client.scans for client in self.clients], [2, 2])

    async def test_manual_leaves_exactly_one_account_and_auto_only_enabled_channel(self):
        await self.scan()
        self.registry.configure("10", True)
        self.assertEqual(self.registry.rules["10"]["due"], self.now + 72 * 3600)
        await self.registry.leave_one("10", automatic=True)
        self.assertEqual(sum(client.leave.await_count for client in self.clients), 0)
        with patch("subscriptions.random.choice", side_effect=lambda items: items[0]):
            self.assertEqual(await self.registry.leave_one("10"), "Аккаунт 1")
        self.assertEqual(self.clients[0].leave.await_args.args[0].channel.channel_id, 10)
        self.assertEqual(self.registry.channel_members()["10"].keys(), {"2"})
        self.now = self.registry.rules["10"]["due"]
        await self.registry.leave_one("11", automatic=True)
        await self.registry.leave_one("10", automatic=True)
        self.assertEqual(self.clients[1].leave.await_count, 1)
        self.assertEqual(self.clients[1].leave.await_args.args[0].channel.channel_id, 10)
        self.assertEqual(self.registry.channel_members()["11"].keys(), {"1"})
        self.registry.configure("10", False)
        restored = Subscriptions(self.directory, lambda: self.accounts, lambda: self.prefs, lambda: self.now)
        self.assertEqual(restored.rules, {})
        self.assertEqual([row["id"] for row in restored.snapshot()["channels"]], ["11"])

    async def test_reschedule_and_flood_wait_preserve_memberships(self):
        await self.scan()
        self.registry.configure("11", True)
        self.prefs.update(subscription_hours=3, subscription_spread=10)
        with patch("subscriptions.random.uniform", return_value=1.1) as sample:
            self.registry.reschedule()
        sample.assert_called_once_with(0.9, 1.1)
        self.assertAlmostEqual(self.registry.rules["11"]["due"], self.now + 11880)
        self.now = self.registry.rules["11"]["due"]
        self.clients[0].leave.side_effect = FloodWaitError(request=None, capture=1800)
        with self.assertRaisesRegex(ValueError, "1800"):
            await self.registry.leave_one("11", automatic=True)
        self.assertIn("1", self.registry.channel_members()["11"])
        self.assertEqual(self.registry.rules["11"]["due"], self.now + 1800)
        self.assertEqual(self.registry.cooldown["1"], self.now + 1800)

    async def test_bulk_plan_excludes_green_channels_and_continues_after_restart(self):
        await self.scan()
        self.registry.eligible = lambda account: False
        app = web.Application()
        app.router.add_post('/mass/{command}', self.registry.handle_bulk)
        data = {'keep': ['11'], 'start': self.now + 60, 'end': self.now + 180}
        async with TestClient(TestServer(app)) as http:
            preview = await (await http.post('/mass/preview', json=data)).json()
            self.assertEqual((preview['leaves'], preview['channels'], preview['accounts']), (2, 1, 2))
            self.assertIsNone(self.registry.bulk)
            self.assertEqual((await http.post('/mass/start', json=data | {'token': 'bad'})).status, 400)
            response = await http.post('/mass/start', json=data | {'token': preview['token']})
            self.assertEqual(response.status, 200)
        await self.registry.bulk_tick()
        self.assertEqual(sum(c.leave.await_count for c in self.clients), 0)
        self.now += 60
        await self.registry.bulk_tick()
        self.assertEqual(sum(c.leave.await_count for c in self.clients), 1)
        restored = Subscriptions(self.directory, lambda: self.accounts, lambda: self.prefs, lambda: self.now)
        self.assertEqual(restored.bulk_status()['done'], 1)
        restored.eligible = lambda account: False
        await restored.bulk_tick()
        self.assertEqual(sum(c.leave.await_count for c in self.clients), 1)
        self.now += 60
        await restored.bulk_tick()
        await restored.bulk_tick()
        self.assertEqual(restored.bulk_status()['status'], 'finished')
        self.assertEqual(restored.bulk_status()['done'], 2)
        self.assertEqual(restored.channel_members()['11'].keys(), {'1'})
        self.assertTrue(all(c.leave.await_args.args[0].channel.channel_id == 10 for c in self.clients))

    async def test_bulk_cooldown_and_expiry_cannot_send_outside_period(self):
        await self.scan()
        job = {'status': 'running', 'start': self.now, 'end': self.now + 20, 'keep': ['10'], 'items': [
            {'channel': '11', 'account': '1', 'status': 'pending', 'due': self.now}
        ]}
        self.registry.bulk = job
        self.clients[0].leave.side_effect = FloodWaitError(request=None, capture=30)
        await self.registry.bulk_tick()
        self.assertEqual(self.registry.bulk_status()['skipped'], 1)
        self.assertIn("1", self.registry.channel_members()["11"])
        self.now += 20
        await self.registry.bulk_tick()
        self.assertIsNone(self.registry.bulk_status()['next_due'])
        self.clients[0].leave.assert_awaited_once()


class JoinViewsTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_records_latest_post_and_optionally_previous(self):
        peer = channel(10, 'Новости', broadcast=True, access_hash=100)
        replies = [
            types.Message(30, types.PeerChannel(10)),
            types.MessageService(29, types.PeerChannel(10), datetime.now(timezone.utc), types.MessageActionEmpty()),
            types.Message(28, types.PeerChannel(10)),
        ]
        for value, expected in ((.25, [30, 28]), (.75, [30])):
            client = AsyncMock(return_value=SimpleNamespace(chats=[peer]))
            client.get_entity.return_value = peer
            client.get_messages.return_value = replies
            with patch('channels.random.random', return_value=value):
                status, note = await channels.ChannelJoiner._join(client, 'public', '@example')
            self.assertEqual(status, 'ok')
            self.assertIn(f'постов: {len(expected)}', note)
            request = client.await_args_list[-1].args[0]
            self.assertIsInstance(request, functions.messages.GetMessagesViewsRequest)
            self.assertTrue(request.increment)
            self.assertEqual(request.id, expected)

    async def test_optional_view_failure_keeps_successful_join(self):
        peer = channel(10, 'Новости', broadcast=True, access_hash=100)
        client = AsyncMock(return_value=SimpleNamespace(chats=[peer]))
        client.get_messages.side_effect = OSError('offline')
        self.assertEqual(await channels.ChannelJoiner._join(client, 'invite', 'invitation'), ('ok', 'вошёл'))
        client.assert_awaited_once()
