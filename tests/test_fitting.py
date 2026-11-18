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
from app.services.fitting_sde import build_market_tree, fits_hull, hull_profile


SDE = {
    "invTypes": [
        {"typeID": "587", "typeName": "Rifter", "groupID": "25", "published": "1"},
        {"typeID": "5870", "typeName": "Fake Ship", "groupID": "25", "published": "0"},
        {"typeID": "123", "typeName": "Damage Control II", "groupID": "60", "published": "1"},
        {"typeID": "778", "typeName": "Scourge Fury Light Missile", "groupID": "81", "published": "1"},
        {"typeID": "999", "typeName": "Invalid Category", "groupID": "99", "published": "1"},
    ],
    "invGroups": [
        {"groupID": "25", "groupName": "Frigate", "categoryID": "6"},
        {"groupID": "60", "groupName": "Armor", "categoryID": "7"},
        {"groupID": "99", "groupName": "Not a Ship", "categoryID": "4"},
        {"groupID": "81", "groupName": "Light Missiles", "categoryID": "8"},
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


class MarketTreeTests(unittest.TestCase):
    def test_nests_items_and_skips_single_root(self):
        groups = {
            1: {"parent": None, "name": "Ship Equipment"},
            2: {"parent": 1, "name": "Propulsion Modules"},
            3: {"parent": 2, "name": "Afterburners"},
        }
        items = [
            {"id": 1, "name": "10MN Afterburner II", "group": "Propulsion", "market_group": 3},
            {"id": 2, "name": "Odd Thing", "group": "Misc", "market_group": None},
        ]
        tree = build_market_tree(items, groups)
        names = [node["name"] for node in tree]
        self.assertEqual(names, ["Ohne Marktgruppe", "Ship Equipment"])
        equipment = tree[1]
        self.assertEqual(equipment["children"][0]["children"][0]["items"][0]["name"], "10MN Afterburner II")
        self.assertEqual(equipment["count"], 1)

    def test_hull_filter_checks_slots_hardpoints_rig_size_and_restrictions(self):
        hull = hull_profile(587, 25, {14: 3, 13: 3, 12: 3, 1137: 3, 102: 3, 101: 0, 1547: 1, 48: 150, 11: 50})
        self.assertTrue(fits_hull({50: 20, 30: 1}, {11}, hull))
        self.assertFalse(fits_hull({}, {12, 40}, hull))  # no launcher hardpoints
        self.assertFalse(fits_hull({}, {3772}, hull))  # no subsystem slots
        self.assertFalse(fits_hull({1547: 2}, {2663}, hull))  # wrong rig size
        self.assertTrue(fits_hull({1547: 1}, {2663}, hull))
        self.assertFalse(fits_hull({50: 999}, {13}, hull))  # CPU too high
        self.assertFalse(fits_hull({1298: 99}, {13}, hull))  # restricted to another ship group
        self.assertTrue(fits_hull({1298: 25}, {13}, hull))
        self.assertTrue(fits_hull({1302: 587}, {13}, hull))


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
            groups = self.client.get("/api/fitting/groups?category=modules")
            charges = self.client.get("/api/fitting/catalog?category=charges&q=fury")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["name"] for item in response.json()["items"]], ["Rifter"])
        self.assertEqual(response.json()["items"][0]["group"], "Frigate")
        self.assertEqual([item["name"] for item in modules.json()["items"]], ["Damage Control II"])
        self.assertIn({"id": 60, "name": "Armor"}, groups.json()["groups"])
        self.assertEqual(charges.json()["items"][0]["name"], "Scourge Fury Light Missile")

    def test_tree_api_nests_market_groups_and_filters_by_hull(self):
        sde = {
            "invTypes": [
                {"typeID": "587", "typeName": "Rifter", "groupID": "25", "published": "1", "marketGroupID": "10"},
                {"typeID": "1", "typeName": "Small Rig", "groupID": "60", "published": "1", "marketGroupID": "3"},
                {"typeID": "2", "typeName": "Medium Rig", "groupID": "60", "published": "1", "marketGroupID": "3"},
            ],
            "invGroups": SDE["invGroups"],
        }
        extra = {
            "market_groups": {
                1: {"parent": None, "name": "Ship Modifications"},
                3: {"parent": 1, "name": "Rigs"},
                10: {"parent": None, "name": "Frigates"},
            },
            "attributes": {587: {1547: 1, 1137: 3}, 1: {1547: 1}, 2: {1547: 2}},
            "effects": {1: {2663}, 2: {2663}},
        }
        with (
            patch("app.main.load_sde", new=AsyncMock(return_value=sde)),
            patch("app.main.load_fitting_sde", new=AsyncMock(return_value=extra)),
        ):
            full = self.client.get("/api/fitting/tree?category=modules")
            filtered = self.client.get("/api/fitting/tree?category=modules&hull_id=587")
            bad = self.client.get("/api/fitting/tree?category=modules&hull_id=1")
        self.assertEqual(full.json()["total"], 2)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertTrue(filtered.json()["filtered"])
        self.assertEqual(filtered.json()["groups"][0]["items"][0]["name"], "Small Rig")
        self.assertEqual(bad.status_code, 422)

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
        self.assertIn('id="fit-catalog-tree"', response.text)
        self.assertIn('data-category="modules"', response.text)
        self.assertIn('id="fit-ship-drop"', response.text)
        self.assertIn("Fit-Datei importieren", response.text)
        self.assertIn("data-load", response.text)
        self.assertIn("draggable=true", response.text)
        self.assertIn('id="fit-hull-only"', response.text)
        self.assertIn("/api/fitting/tree", response.text)
        self.assertIn("application/x-eve-fitting", response.text)

    def test_fitting_module_is_registered_for_admin_activation(self):
        self.assertIn("fitting", {item["name"] for item in main.storage.list_modules()})


if __name__ == "__main__":
    unittest.main()
