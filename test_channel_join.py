import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl import functions, types

import channels


def channel(identity, title, **fields):
    return types.Channel(id=identity, title=title, photo=types.ChatPhotoEmpty(),
                         date=datetime.now(timezone.utc), **fields)


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
