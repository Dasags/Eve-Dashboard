import atexit
import os
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if "DATA_DIR" not in os.environ:
    _test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-storage-tests-")
    os.environ["DATA_DIR"] = _test_data_dir
    atexit.register(shutil.rmtree, _test_data_dir, ignore_errors=True)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

from app.services import storage


class StorageTests(unittest.TestCase):
    def test_initialize_migrates_existing_blueprint_rows(self):
        with tempfile.TemporaryDirectory(prefix="eve-dashboard-bpo-migration-") as directory:
            data_dir = Path(directory)
            database = data_dir / "dashboard.sqlite3"
            with sqlite3.connect(database) as db:
                db.execute(
                    "CREATE TABLE bpo_library ("
                    "character_id INTEGER NOT NULL, item_id INTEGER NOT NULL, type_id INTEGER NOT NULL, "
                    "type_name TEXT NOT NULL, group_name TEXT NOT NULL, is_copy INTEGER NOT NULL DEFAULT 0, "
                    "runs INTEGER, material_efficiency INTEGER, time_efficiency INTEGER, "
                    "is_shared INTEGER NOT NULL DEFAULT 0, synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "PRIMARY KEY(character_id, item_id))"
                )
                db.execute(
                    "INSERT INTO bpo_library(character_id,item_id,type_id,type_name,group_name) "
                    "VALUES (1,11,101,'Blueprint','Blueprints')"
                )
            with patch.object(storage, "DATA_DIR", data_dir), patch.object(storage, "DATABASE", database):
                storage.initialize()
                with storage.connection() as db:
                    columns = {row["name"] for row in db.execute("PRAGMA table_info(bpo_library)")}
                    row = db.execute(
                        "SELECT item_id, location_name, container_name FROM bpo_library"
                    ).fetchone()
            self.assertTrue({"location_id", "location_name", "location_flag", "container_name"} <= columns)
            self.assertEqual(row["item_id"], 11)
            self.assertEqual(row["location_name"], "")
            self.assertEqual(row["container_name"], "")

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

    def test_blueprint_share_survives_inventory_refresh_and_removed_assets_are_unshared(self):
        account_id = storage.save_character(
            987654329,
            "Blueprint Owner",
            {"access_token": "a", "refresh_token": "r", "scope": [], "expires_at": 1},
        )
        blueprint = {
            "item_id": 7001,
            "type_id": 1001,
            "type_name": "Acolyte Blueprint",
            "group_name": "Drone Blueprints",
            "is_copy": False,
            "runs": None,
            "material_efficiency": 10,
            "time_efficiency": 20,
            "location_id": 60003760,
            "location_name": "Jita IV - Moon 4",
            "location_flag": "Hangar",
            "container_name": "Blueprint Container",
        }
        storage.sync_blueprint_library(987654329, [blueprint])
        self.assertTrue(storage.set_blueprint_shared(987654329, 7001, True))
        updated = {**blueprint, "material_efficiency": 12}
        storage.sync_blueprint_library(987654329, [updated])
        own = storage.blueprint_library(987654329)
        shared = storage.shared_blueprint_library()
        self.assertEqual(own[0]["material_efficiency"], 12)
        self.assertEqual(own[0]["is_shared"], 1)
        self.assertEqual(own[0]["location_name"], "Jita IV - Moon 4")
        self.assertEqual(own[0]["container_name"], "Blueprint Container")
        self.assertEqual(shared[0]["character_name"], "Blueprint Owner")
        self.assertNotIn("container_name", shared[0])
        self.assertEqual(shared[0]["account_id"], account_id)
        storage.sync_blueprint_library(987654329, [])
        self.assertEqual(storage.blueprint_library(987654329), [])
        self.assertEqual(storage.shared_blueprint_library(), [])
        self.assertTrue(storage.delete_account(account_id))


class AccountModuleTests(unittest.TestCase):
    def test_module_grants_are_per_account(self):
        token = {"access_token": "a", "refresh_token": "r", "scope": [], "expires_at": 1}
        first = storage.save_character(111, "First", token)
        second = storage.save_character(222, "Second", token)
        self.assertFalse(storage.module_allowed(first, "wallet"))
        self.assertTrue(storage.set_account_modules(first, {"wallet"}))
        self.assertTrue(storage.module_allowed(first, "wallet"))
        self.assertFalse(storage.module_allowed(second, "wallet"))
        self.assertFalse(storage.set_account_modules(999999, {"wallet"}))
        storage.set_account_modules(first, set())
        self.assertFalse(storage.module_allowed(first, "wallet"))


if __name__ == "__main__":
    unittest.main()
