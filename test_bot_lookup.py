import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telethon import errors, functions, types

from bot_lookup import BotUnavailable, resolve_bot


class BotLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_bot_from_same_session_recovers_username_error(self):
        client = AsyncMock()
        peer = types.InputPeerUser(42, 123)
        client.session = SimpleNamespace(get_input_entity=Mock(return_value=peer))
        bot = types.User(42, bot=True, username="examplebot")
        client.get_entity.side_effect = [ValueError('No user has "examplebot" as username'), bot]
        self.assertIs(await resolve_bot(client, "@ExampleBot"), bot)
        self.assertEqual(client.get_entity.await_args_list[-1].args, (peer,))
        self.assertIs(await resolve_bot(client, "examplebot"), bot)
        self.assertEqual(client.get_entity.await_count, 2)
        client.assert_not_awaited()
        client.get_dialogs.assert_not_awaited()

    async def test_stale_peer_and_similar_names_are_ignored(self):
        client = AsyncMock()
        client.session = SimpleNamespace(get_input_entity=Mock(return_value=types.InputPeerUser(42, 123)))
        stale = types.User(42, bot=True, username="oldbot")
        client.get_entity.side_effect = [ValueError('No user has "examplebot" as username'), stale]
        client.return_value = SimpleNamespace(users=[types.User(6, bot=True, username="examplebot2")])
        matching = types.User(88, bot=True, username="primarybot",
                              usernames=[types.Username("ExampleBot", active=True)])
        client.get_dialogs.return_value = [SimpleNamespace(entity=matching)]
        self.assertIs(await resolve_bot(client, "examplebot"), matching)
        self.assertIsInstance(client.await_args.args[0], functions.contacts.SearchRequest)

    async def test_missing_bot_cache_is_scoped_to_each_client(self):
        missing, working = AsyncMock(), AsyncMock()
        missing.session = None
        missing.get_entity.side_effect = errors.UsernameNotOccupiedError(request=None)
        missing.return_value = SimpleNamespace(users=[])
        missing.get_dialogs.return_value = []
        with self.assertRaisesRegex(BotUnavailable, "@examplebot"):
            await resolve_bot(missing, "examplebot")
        real = types.User(7, bot=True, username="examplebot")
        working.get_entity.return_value = real
        self.assertIs(await resolve_bot(working, "examplebot"), real)
        with self.assertRaises(BotUnavailable):
            await resolve_bot(missing, "examplebot")
        missing.assert_awaited_once()

    async def test_flood_wait_does_not_trigger_more_lookup_requests(self):
        client = AsyncMock()
        client.get_entity.side_effect = errors.FloodWaitError(request=None, capture=60)
        with self.assertRaises(errors.FloodWaitError):
            await resolve_bot(client, "examplebot")
        client.assert_not_awaited()
        client.get_dialogs.assert_not_awaited()
