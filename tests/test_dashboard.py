import atexit
import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import httpx
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("COOKIE_SECURE", "false")
if "DATA_DIR" not in os.environ:
    test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-page-tests-")
    os.environ["DATA_DIR"] = test_data_dir
    atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

from app import main


SCOPE = "esi-wallet.read_character_wallet.v1"
CHARS = [
    {"id": 101, "name": "Main Pilot", "is_main": True},
    {"id": 102, "name": "Alt Pilot", "is_main": False},
    {"id": 103, "name": "No Scope Pilot", "is_main": False},
    {"id": 104, "name": "Offline Pilot", "is_main": False},
]


class MockESI:
    calls = []

    def __init__(self, character_id=None):
        self.character_id = character_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path):
        self.calls.append((self.character_id, path))
        if self.character_id is None:
            char_id = int(path.strip("/").split("/")[-1])
            return {"name": next(char["name"] for char in CHARS if char["id"] == char_id), "security_status": 0.1}
        if self.character_id == 104:
            request = httpx.Request("GET", "https://esi.evetech.net/test")
            raise httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
        return {101: 1234567.89, 102: 0}[self.character_id]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        session = base64.b64encode(json.dumps({"account_id": 1, "character_id": 101}).encode())
        self.client.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(session).decode())
        MockESI.calls = []
        patches = [
            patch("app.main.require_module"),
            patch("app.main.storage.character_belongs_to", side_effect=lambda character_id, account_id: account_id == 1 and character_id in {101, 102, 103, 104}),
            patch("app.main.storage.list_characters", return_value=CHARS),
            patch("app.main.storage.get_character", side_effect=lambda character_id: {"scopes": [] if character_id == 103 else [SCOPE]}),
            patch("app.main.ESIClient", MockESI),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_balances_are_per_character_with_independent_failures(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("CHAR-TERMINAL // CHARAKTERÜBERSICHT", response.text)
        self.assertIn("1.234.567,89 ISK", response.text)
        self.assertIn("0,00 ISK", response.text)
        self.assertIn("Wallet-Berechtigung fehlt", response.text)
        self.assertIn("Kontostand momentan nicht verfügbar", response.text)
        self.assertIn((101, "/characters/101/wallet/"), MockESI.calls)
        self.assertIn((102, "/characters/102/wallet/"), MockESI.calls)
        self.assertIn((104, "/characters/104/wallet/"), MockESI.calls)
        self.assertNotIn((103, "/characters/103/wallet/"), MockESI.calls)
        self.assertIn("Skill-Warteschlange", response.text)

    def test_character_selection_still_switches_dashboard(self):
        response = self.client.post("/auth/chars/select", data={"character_id": 102, "next": "/dashboard"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/dashboard")
        dashboard = self.client.get("/dashboard")
        self.assertIn("<h1>Alt Pilot</h1>", dashboard.text)
        self.assertIn("Alt-Charakter", dashboard.text)
        self.assertIn("1.234.567,89 ISK", dashboard.text)


if __name__ == "__main__":
    unittest.main()
