import atexit
import os
import shutil
import tempfile
import unittest

if "DATA_DIR" not in os.environ:
    _test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-storage-tests-")
    os.environ["DATA_DIR"] = _test_data_dir
    atexit.register(shutil.rmtree, _test_data_dir, ignore_errors=True)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

from app.services import storage


class StorageTests(unittest.TestCase):
    def test_tokens_are_encrypted_and_characters_are_account_scoped(self):
        token = {
            "access_token": "private-access-token",
            "refresh_token": "private-refresh-token",
            "scope": ["esi-wallet.read_character_wallet.v1"],
            "expires_at": 1234567890,
        }
        account_id = storage.save_character(987654321, "Main Pilot", token)
        self.assertEqual(
            storage.save_character(987654322, "Alt Pilot", token, account_id),
            account_id,
        )

        with storage.connection() as db:
            encrypted = db.execute(
                "SELECT token FROM characters WHERE id = ?", (987654321,)
            ).fetchone()["token"]
        self.assertNotIn(b"private-access-token", encrypted)
        self.assertEqual(storage.get_character(987654321)["token"], token)
        self.assertEqual(
            [item["is_main"] for item in storage.list_characters(account_id)],
            [True, False],
        )
        self.assertFalse(storage.remove_character(987654321, account_id))
        self.assertTrue(storage.remove_character(987654322, account_id))
        self.assertTrue(storage.delete_account(account_id))


if __name__ == "__main__":
    unittest.main()
