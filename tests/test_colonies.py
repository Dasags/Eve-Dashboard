import os
import atexit
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("COOKIE_SECURE", "false")
if "DATA_DIR" not in os.environ:
    test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-colony-tests-")
    os.environ["DATA_DIR"] = test_data_dir
    atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

from fastapi.testclient import TestClient

from app import main
from app.services.colonies import SCOPE, factory_rings, summarize

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SUMMARY = {"planet_id": 5, "solar_system_id": 9, "planet_type": "temperate", "upgrade_level": 5, "num_pins": 3}


def detail(*expiries):
    pins = [
        {"extractor_details": {"product_type_id": 7, "heads": [{}, {}]}, "expiry_time": e}
        for e in expiries
    ]
    pins.append({"factory_details": {"schematic_id": 70}})
    return {"pins": pins}


class ColonySummaryTests(unittest.TestCase):
    def test_running_soon_and_expired_status(self):
        running = summarize(SUMMARY, detail("2026-01-03T00:00:00Z"), NOW)
        soon = summarize(SUMMARY, detail("2026-01-01T05:00:00Z", "2026-01-04T00:00:00Z"), NOW)
        expired = summarize(SUMMARY, detail("2025-12-31T00:00:00Z"), NOW)
        self.assertEqual(running["status"], "running")
        self.assertEqual(soon["status"], "soon")
        self.assertEqual(soon["seconds_left"], 5 * 3600)
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(running["factories"], {70: 1})
        self.assertEqual(running["extractors"][0]["heads"], 2)

    def test_factory_ring_progress(self):
        colony = summarize(
            SUMMARY,
            {"pins": [
                {"factory_details": {"schematic_id": 70}, "last_cycle_start": "2026-01-01T00:00:00Z"},
                {"factory_details": {"schematic_id": 70}, "last_cycle_start": "2025-12-31T00:00:00Z"},
                {"factory_details": {"schematic_id": 70}},
            ]},
            NOW,
        )
        rings = factory_rings(colony, {70: 3600}, {70: "Synthetic Oil"}, datetime(2026, 1, 1, 0, 15, tzinfo=timezone.utc))
        by_status = {r["status"]: r for r in rings}
        self.assertEqual(by_status["running"]["seconds_left"], 2700)
        self.assertAlmostEqual(by_status["running"]["fraction"], 0.75)
        self.assertEqual(by_status["expired"]["fraction"], 0)
        self.assertEqual(by_status["idle"]["seconds_left"], None)

    def test_colony_without_extractors_is_idle(self):
        self.assertEqual(summarize(SUMMARY, {"pins": []}, NOW)["status"], "idle")


class ColonyPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.patches = [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
            patch("app.main.pi_products", new=AsyncMock(return_value=[])),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_missing_scope_asks_for_new_login(self):
        with patch("app.main.storage.get_character", return_value={"scopes": []}):
            response = self.client.get("/planetary-industry")
        self.assertIn(SCOPE, response.text)
        self.assertIn("erneut anmelden", response.text)

    def test_colonies_render_with_timer_and_status(self):
        colonies = [
            {"planet_id": 5, "system_id": 9, "planet_type": "Temperate", "planet_type_key": "temperate",
             "command_center_level": 5, "pin_count": 3, "factories": {70: 2}, "factory_pins": [{"schematic_id": 70, "last_cycle_start": datetime.now(timezone.utc)}, {"schematic_id": 70, "last_cycle_start": None}], "seconds_left": 3600 * 50,
             "status": "running", "expiry_time": None,
             "extractors": [{"product_type_id": 7, "seconds_left": 3600 * 50, "heads": 4, "expiry_time": None}]},
            {"planet_id": 6, "system_id": 9, "planet_type": "Lava", "planet_type_key": "lava",
             "command_center_level": 4, "pin_count": 2, "factories": {}, "seconds_left": -5,
             "status": "expired", "expiry_time": None,
             "extractors": [{"product_type_id": 7, "seconds_left": -5, "heads": 1, "expiry_time": None}]},
        ]
        esi = AsyncMock()
        async def fake_get(path, **kwargs):
            if "/universe/planets/5/" in path:
                return {"name": "Alentene VII"}
            if "/universe/planets/6/" in path:
                return {"name": "Alentene II"}
            return {"schematic_name": "Synthetic Oil", "cycle_time": 3600}

        esi.get = AsyncMock(side_effect=fake_get)
        esi.__aenter__.return_value = esi
        names = {9: "Alentene", 7: "Proteins"}
        with (
            patch("app.main.storage.get_character", return_value={"scopes": [SCOPE]}),
            patch("app.main.ESIClient", return_value=esi),
            patch("app.main.load_colonies", new=AsyncMock(return_value=colonies)),
            patch("app.main.resolve_names", new=AsyncMock(return_value=names)),
        ):
            response = self.client.get("/planetary-industry")
            second = self.client.get("/planetary-industry?planet=6")
            calc = self.client.get("/planetary-industry?tab=calc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("PI-TERMINAL", response.text)
        self.assertIn("Alentene VII", response.text)
        self.assertIn("CC-Stufe 5", response.text)
        self.assertIn("2d 2h 0m", response.text)
        self.assertIn("Fabriken (2)", response.text)
        self.assertIn("Synthetic Oil", response.text)
        self.assertIn("kein Zyklus aktiv", response.text)
        self.assertIn("abgelaufen", response.text)
        self.assertIn("CC-Stufe 4", second.text)
        self.assertNotIn("PI-TERMINAL", calc.text)
        self.assertIn("Produktionskette berechnen", calc.text)

    def test_forbidden_esi_shows_scope_hint(self):
        import httpx

        request = httpx.Request("GET", "https://esi.evetech.net/x")
        error = httpx.HTTPStatusError("403", request=request, response=httpx.Response(403, request=request))
        with (
            patch("app.main.storage.get_character", return_value={"scopes": [SCOPE]}),
            patch("app.main.load_colonies", new=AsyncMock(side_effect=error)),
        ):
            response = self.client.get("/planetary-industry")
        self.assertIn("HTTP 403", response.text)

    def test_other_esi_status_is_shown(self):
        import httpx

        request = httpx.Request("GET", "https://esi.evetech.net/x")
        error = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
        with (
            patch("app.main.storage.get_character", return_value={"scopes": [SCOPE]}),
            patch("app.main.load_colonies", new=AsyncMock(side_effect=error)),
        ):
            response = self.client.get("/planetary-industry")
        self.assertIn("HTTP 500", response.text)


if __name__ == "__main__":
    unittest.main()
