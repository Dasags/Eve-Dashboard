import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


class ProductionPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.characters = [
            {"id": 123, "name": "Main Pilot", "is_main": True},
            {"id": 456, "name": "Alt Pilot", "is_main": False},
        ]
        patches = [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
            patch("app.main.current_account", return_value=1),
            patch("app.main.storage.list_characters", return_value=self.characters),
            patch("app.main.storage.list_modules", return_value=[]),
            patch("app.main.storage.account_module_names", return_value=set()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_industry_renders_terminal_and_character_tiles(self):
        response = self.client.get("/industry")
        self.assertEqual(response.status_code, 200)
        self.assertIn("INDUSTRIE-TERMINAL // AUFTRAG", response.text)
        self.assertIn("INDUSTRIE-TERMINAL // BLUEPRINT", response.text)
        self.assertIn("INDUSTRIE-TERMINAL // STANDORT", response.text)
        self.assertIn('name="next" value="/industry"', response.text)
        self.assertIn('name="product_name"', response.text)

    def test_industry_is_an_allowed_character_switch_destination(self):
        with patch("app.main.storage.character_belongs_to", return_value=True):
            response = self.client.post(
                "/auth/chars/select",
                data={"character_id": 456, "next": "/industry"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/industry")


if __name__ == "__main__":
    unittest.main()
