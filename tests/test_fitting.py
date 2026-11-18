import os
import atexit
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("COOKIE_SECURE", "false")
if "DATA_DIR" not in os.environ:
    test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-fitting-tests-")
    os.environ["DATA_DIR"] = test_data_dir
    atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

from fastapi.testclient import TestClient

from app import main
from app.services.fitting import fitting_details


SDE = {
    "invTypes": [
        {"typeID": "587", "typeName": "Rifter", "groupID": "25", "published": "1"},
        {"typeID": "5870", "typeName": "Fake Ship", "groupID": "25", "published": "0"},
        {"typeID": "123", "typeName": "Damage Control II", "groupID": "60", "published": "1"},
        {"typeID": "777", "typeName": "Scourge Light Missile", "groupID": "80", "published": "1"},
        {"typeID": "999", "typeName": "Invalid Category", "groupID": "99", "published": "1"},
    ],
    "invGroups": [
        {"groupID": "25", "groupName": "Frigate", "categoryID": "6"},
        {"groupID": "60", "groupName": "Armor", "categoryID": "7"},
        {"groupID": "99", "groupName": "Not a Ship", "categoryID": "4"},
        {"groupID": "80", "groupName": "Light Missiles", "categoryID": "8"},
    ],
}


class FittingServiceTests(unittest.TestCase):
    def test_extracts_ship_attributes_and_module_requirements_and_slot(self):
        ship = fitting_details(
            587,
            {"name": "Rifter", "dogma_attributes": [
                {"attribute_id": 11, "value": 55},
                {"attribute_id": 48, "value": 160},
                {"attribute_id": 14, "value": 4},
                {"attribute_id": 1367, "value": 0},
            ]},
            "ship",
        )
        self.assertEqual(ship["attributes"]["powergrid"], 55)
        self.assertEqual(ship["attributes"]["cpu"], 160)
        self.assertEqual(ship["attributes"]["high_slots"], 4)
        module = fitting_details(
            123,
            {"name": "Damage Control II",
             "dogma_attributes": [
                 {"attribute_id": 50, "value": 30},
                 {"attribute_id": 30, "value": 1},
             ],
             "dogma_effects": [{"name": "loPower"}]},
            "module",
        )
        self.assertEqual(module["slot"], "low")
        self.assertEqual(module["requirements"], {"cpu": 30, "powergrid": 1, "calibration": 0})


class FittingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.patches = [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_catalog_filters_by_category_published_and_query(self):
        with patch("app.main.load_sde", new=AsyncMock(return_value=SDE)):
            response = self.client.get("/api/fitting/catalog?category=ships&q=rift")
            modules = self.client.get("/api/fitting/catalog?category=modules&q=damage")
            charges = self.client.get("/api/fitting/catalog?category=charges&q=scourge")
            groups = self.client.get("/api/fitting/groups?category=modules")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.json()["items"]], ["Rifter"])
        self.assertEqual(response.json()["items"][0]["group"], "Frigate")
        self.assertEqual([item["name"] for item in modules.json()["items"]], ["Damage Control II"])
        self.assertEqual([item["name"] for item in charges.json()["items"]], ["Scourge Light Missile"])
        self.assertEqual(groups.json()["groups"][0], {"id": 60, "name": "Armor"})

    def test_type_api_returns_validated_esi_dogma(self):
        esi = AsyncMock()
        esi.__aenter__.return_value = esi
        esi.get = AsyncMock(return_value={
            "name": "Rifter",
            "dogma_attributes": [{"attribute_id": 11, "value": 55}],
            "dogma_effects": [],
        })
        with (
            patch("app.main.load_sde", new=AsyncMock(return_value=SDE)),
            patch("app.main.ESIClient", return_value=esi),
        ):
            response = self.client.get("/api/fitting/type/587?category=ship")
            invalid = self.client.get("/api/fitting/type/999?category=ship")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attributes"]["powergrid"], 55)
        self.assertEqual(invalid.status_code, 422)

    def test_page_requires_module_and_renders_tool(self):
        with patch("app.main.require_module"):
            response = self.client.get("/fitting")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fleet-Analyse", response.text)
        self.assertIn("Fitting zur Flotte", response.text)
        self.assertIn("lokal in diesem Browser", response.text)
        self.assertIn('id="fit-category"', response.text)
        self.assertIn('id="fit-group"', response.text)
        self.assertIn('id="fit-import-file"', response.text)
        self.assertIn("Fit laden", response.text)

    def test_fitting_module_is_registered_for_admin_activation(self):
        self.assertIn("fitting", {item["name"] for item in main.storage.list_modules()})


if __name__ == "__main__":
    unittest.main()
