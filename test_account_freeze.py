import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telethon import functions, types
from telethon.errors import FrozenMethodInvalidError

import main


def app_config(**values):
    return types.help.AppConfig(hash=1, config=types.JsonObject([
        types.JsonObjectValue(key, types.JsonNumber(value)) for key, value in values.items()
    ]))


class FreezeDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_session_connects_but_is_not_available_to_services(self):
        client = AsyncMock(return_value=app_config(freeze_since_date=1760000000, freeze_until_date=1762000000))
        client.is_connected = lambda: True
        client.get_me.return_value = types.User(10, first_name="Test")
        acc = main.Account("frozen", client)
        acc.handler_registered = True
        with patch.multiple(main, ACCOUNTS={"frozen": acc}, ACCOUNT_CONNECT_SEMAPHORE=asyncio.Semaphore(1),
                            ACCOUNT_FLAGS=None, INDEX={}):
            self.assertTrue(await main.connect_account(acc))
            self.assertEqual(acc.status, "Заморожен")
            item = main.ser_account(acc)
            self.assertTrue(item["connected"])
            self.assertTrue(item["frozen"])
            self.assertEqual(item["freeze"], {"frozen": True, "since": 1760000000, "until": 1762000000})
            self.assertEqual(main.service_clients(), {})
        client.assert_awaited_once()
        request = client.await_args.args[0]
        self.assertIsInstance(request, functions.help.GetAppConfigRequest)
        self.assertEqual(request.hash, 0)
        client.send_message.assert_not_awaited()

    async def test_clear_config_and_unknown_check_are_distinct(self):
        client = AsyncMock(return_value=app_config())
        self.assertEqual(await main.check_account_freeze(client), {"frozen": False})
        client.return_value = types.help.AppConfigNotModified()
        self.assertIsNone(await main.check_account_freeze(client))
        client.side_effect = TimeoutError()
        self.assertIsNone(await main.check_account_freeze(client))
        client.side_effect = FrozenMethodInvalidError(request=None)
        self.assertEqual(await main.check_account_freeze(client), {"frozen": True})

    async def test_freeze_rpc_error_on_get_me_is_displayed_without_reconnect(self):
        client = AsyncMock()
        client.get_me.side_effect = FrozenMethodInvalidError(request=None)
        acc = main.Account("frozen", client)
        with patch.object(main, "ACCOUNT_CONNECT_SEMAPHORE", asyncio.Semaphore(1)):
            self.assertFalse(await main.connect_account(acc))
        self.assertTrue(main.is_frozen(acc))
        self.assertEqual(acc.error_kind, "frozen")
        self.assertIsNone(acc.next_retry_at)
