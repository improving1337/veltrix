import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

import main


class ImportTests(unittest.TestCase):
    def test_standalone_session_defaults_and_existing_config_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts_dir, sessions_dir = root / 'Account', root / 'Session'
            accounts_dir.mkdir()
            sessions_dir.mkdir()
            for name in ['standalone', 'custom']:
                session = SQLiteSession(str(sessions_dir / name))
                session.auth_key = AuthKey(bytes(range(256)))
                session.save()
                session.close()
            custom = {'app_id': 12345, 'app_hash': 'custom-hash'}
            (accounts_dir / 'custom.json').write_text(json.dumps(custom))
            accounts = {}
            with patch.multiple(main, ACCOUNT_DIR=accounts_dir, SESSION_DIR=sessions_dir,
                                ACCOUNTS=accounts, ACCOUNT_LOAD_ERRORS={}):
                try:
                    added = main.load_accounts()
                    self.assertEqual(len(added), 2)
                    self.assertEqual(accounts['standalone'].client.api_id, 2040)
                    self.assertEqual(accounts['custom'].client.api_id, 12345)
                    self.assertEqual(json.loads((accounts_dir / 'custom.json').read_text()), custom)
                    self.assertEqual(main.load_accounts(), [])
                finally:
                    for account in accounts.values():
                        account.client.session.close()


if __name__ == '__main__':
    unittest.main()
