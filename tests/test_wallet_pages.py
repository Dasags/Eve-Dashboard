import atexit
import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("COOKIE_SECURE", "false")
if "DATA_DIR" not in os.environ:
    test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-wallet-tests-")
    os.environ["DATA_DIR"] = test_data_dir
    atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from app import main


CHARACTERS = [
    {"id": 101, "name": "Main Pilot", "is_main": True},
    {"id": 102, "name": "Alt Pilot", "is_main": False},
]
SCOPE = "esi-wallet.read_character_wallet.v1"


class WalletPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        session = base64.b64encode(json.dumps({"account_id": 1, "character_id": 101}).encode())
        self.client.cookies.set("session", TimestampSigner(os.environ["SESSION_SECRET"]).sign(session).decode())
        patches = [
            patch("app.main.require_module"),
            patch("app.main.storage.character_belongs_to", side_effect=lambda character_id, account_id: character_id in {101, 102} and account_id == 1),
            patch("app.main.storage.list_characters", return_value=CHARACTERS),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_character_tiles_switch_selected_pilot_on_wallet_and_distribution(self):
        plan = {"start_balance": 0, "entries": [], "labels": []}
        with (
            patch("app.main.storage.get_character", return_value={"scopes": []}),
            patch("app.main.storage.wallet_plan", return_value=plan),
            patch("app.main.storage.wallet_overview", return_value=[]),
        ):
            wallet = self.client.get("/wallet")
            self.assertIn('name="next" value="/wallet"', wallet.text)
            self.assertIn('aria-pressed="true"', wallet.text)
            self.assertIn("Wallet-Berechtigung fehlt", wallet.text)
            switched = self.client.post("/auth/chars/select", data={"character_id": 102, "next": "/wallet"}, follow_redirects=False)
            self.assertEqual(switched.headers["location"], "/wallet")
            self.assertIn("Wallet – Alt Pilot", self.client.get("/wallet").text)
            distribution = self.client.get("/wallet/distribution")
            self.assertIn('name="next" value="/wallet/distribution"', distribution.text)
            self.assertIn("Wallet-Verteilung – Alt Pilot", distribution.text)
            self.assertIn("pi-terminal", distribution.text)
            self.assertIn('action="/wallet/plan/entries"', distribution.text)
            switched_back = self.client.post("/auth/chars/select", data={"character_id": 101, "next": "/wallet/distribution"}, follow_redirects=False)
            self.assertEqual(switched_back.headers["location"], "/wallet/distribution")
            self.assertIn("Wallet-Verteilung – Main Pilot", self.client.get("/wallet/distribution").text)

    def test_wallet_journal_and_transactions_keep_data(self):
        esi = AsyncMock()
        esi.__aenter__.return_value = esi
        esi.get.side_effect = lambda path: (
            1234.5 if path.endswith("/wallet/") else
            [{"date": "2026-01-01T10:00:00Z", "is_buy": True, "type_id": 22, "location_id": 33, "quantity": 2, "unit_price": 5}]
        )
        journal = [{"date": "2026-01-01T10:00:00Z", "ref_type": "market_transaction", "amount": -50, "balance": 500}]
        with (
            patch("app.main.storage.get_character", return_value={"scopes": [SCOPE]}),
            patch("app.main.ESIClient", return_value=esi),
            patch("app.main.paginated_list", new=AsyncMock(return_value=journal)),
            patch("app.main.resolve_names", new=AsyncMock(return_value={22: "Tritanium", 33: "Jita"})),
        ):
            wallet = self.client.get("/wallet")
            transactions = self.client.get("/wallet?view=transactions")
        self.assertIn("WALLET-TERMINAL // KONTOSTAND", wallet.text)
        self.assertIn("Market Transaction", wallet.text)
        self.assertIn("Marktgeschäfte", wallet.text)
        self.assertIn("Tritanium · Jita", transactions.text)
        self.assertIn("Kauf", transactions.text)

    def test_esi_error_still_visible_with_character_tiles(self):
        with (
            patch("app.main.storage.get_character", return_value={"scopes": [SCOPE]}),
            patch("app.main.ESIClient", side_effect=RuntimeError("unavailable")),
        ):
            response = self.client.get("/wallet")
        self.assertIn("Wallet konnte nicht geladen werden", response.text)
        self.assertIn("Alt Pilot", response.text)
        self.assertIn('name="next" value="/wallet"', response.text)


if __name__ == "__main__":
    unittest.main()
