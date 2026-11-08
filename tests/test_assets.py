import os
import asyncio
import atexit
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("COOKIE_SECURE", "false")
if "DATA_DIR" not in os.environ:
    test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-assets-tests-")
    os.environ["DATA_DIR"] = test_data_dir
    atexit.register(shutil.rmtree, test_data_dir, ignore_errors=True)

from fastapi.testclient import TestClient

from app import main
from app.services.assets import assemble_assets, blueprint_records, blueprint_types
from app.services.market import sell_prices


class AssetLayoutTests(unittest.TestCase):
    def test_extracts_bpos_and_bpcs_with_me_te_runs_by_group(self):
        sde = {
            "invTypes": [
                {"typeID": "1", "typeName": "Acolyte Blueprint", "groupID": "10"},
                {"typeID": "2", "typeName": "Dragonfly Blueprint", "groupID": "10"},
                {"typeID": "3", "typeName": "Rifter", "groupID": "20"},
            ],
            "invGroups": [
                {"groupID": "10", "groupName": "Frigate Blueprints", "categoryID": "9"},
                {"groupID": "20", "groupName": "Frigates", "categoryID": "6"},
            ],
        }
        assets = [
            {"item_id": 11, "type_id": 1, "is_blueprint_copy": False, "blueprint_material_efficiency": 10, "blueprint_time_efficiency": 20},
            {"item_id": 12, "type_id": 2, "is_blueprint_copy": True, "blueprint_runs": 5, "blueprint_material_efficiency": 8, "blueprint_time_efficiency": 16},
            {"item_id": 13, "type_id": 3},
        ]
        result = blueprint_records(assets, sde)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["group_name"], "Frigate Blueprints")
        self.assertEqual(result[0]["material_efficiency"], 10)
        self.assertEqual(result[0]["time_efficiency"], 20)
        self.assertFalse(result[0]["is_copy"])
        self.assertTrue(result[1]["is_copy"])
        self.assertEqual(result[1]["runs"], 5)
        self.assertEqual(result[1]["material_efficiency"], 8)
        self.assertEqual(result[1]["time_efficiency"], 16)

    def test_nested_items_and_missing_locations(self):
        items = [
            {"item_id": 1, "type_id": 10, "quantity": 1, "location_id": 600001, "location_type": "station"},
            {"item_id": 2, "type_id": 11, "quantity": 3, "location_id": 1, "location_type": "item"},
            {"item_id": 3, "type_id": 12, "quantity": 5, "location_id": 999999, "location_type": "item"},
            {"item_id": 4, "type_id": 13, "quantity": 1, "location_id": 5, "location_type": "item"},
            {"item_id": 5, "type_id": 14, "quantity": 1, "location_id": 4, "location_type": "item"},
        ]
        rows = assemble_assets(items, {11: "Modul"}, {600001: "Jita"})
        self.assertEqual(rows[1]["location_name"], "Jita")
        self.assertEqual(rows[1]["quantity"], 3)
        self.assertTrue(rows[2]["location_missing"])
        self.assertIn("999999", rows[2]["location_name"])
        self.assertTrue(rows[3]["location_missing"])

    def test_asset_pagination_fetches_all_esi_pages(self):
        class Response:
            headers = {"X-Pages": "2"}

            def json(self):
                return [{"item_id": 1}]

        class Client:
            request = AsyncMock(return_value=Response())
            get = AsyncMock(return_value=[{"item_id": 2}])

        items = asyncio.run(main.paginated_list(Client(), "/characters/1/assets/"))
        self.assertEqual([item["item_id"] for item in items], [1, 2])

    def test_asset_copy_flag_is_preserved(self):
        rows = assemble_assets([
            {"item_id": 1, "type_id": 10, "quantity": 1, "location_id": 6, "is_blueprint_copy": True},
            {"item_id": 2, "type_id": 10, "quantity": 1, "location_id": 6},
            {"item_id": 3, "type_id": 10, "quantity": 1, "location_id": 6, "is_blueprint_copy": False},
        ], {}, {})
        self.assertEqual([row["is_blueprint_copy"] for row in rows], [True, False, False])

    def test_blueprint_categories_and_unavailable_types(self):
        async def get(path):
            values = {
                "/universe/types/10/": {"group_id": 5},
                "/universe/types/11/": {"group_id": 5},
                "/universe/types/12/": {"group_id": 6},
                "/universe/groups/5/": {"category_id": 9},
                "/universe/groups/6/": {"category_id": 6},
            }
            if path not in values:
                raise ValueError("unavailable")
            return values[path]

        client = type("Client", (), {"get": AsyncMock(side_effect=get)})()
        result = asyncio.run(blueprint_types(client, {10, 11, 12, 13}))
        self.assertEqual(result, {10: True, 11: True, 12: False, 13: None})
        self.assertEqual(client.get.await_count, 6)


class AssetRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.patches = [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
            patch("app.main.blueprint_types", new=AsyncMock(return_value={})),
            patch("app.main.storage.get_character", return_value={"scopes": ["esi-assets.read_assets.v1"]}),
            patch("app.main.storage.list_locations", return_value=[
                {"id": 1, "name": "Jita", "location_id": 60003760, "is_default": 1},
                {"id": 2, "name": "Heimat", "location_id": 60000001, "is_default": 0},
            ]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_filter_and_unresolved_structure(self):
        inventory = [
            {"item_id": 1, "type_id": 10, "quantity": 3, "location_id": 60003760, "location_type": "station"},
            {"item_id": 2, "type_id": 11, "quantity": 2, "location_id": 1000000001234, "location_type": "other"},
        ]
        with patch("app.main.paginated_list", new=AsyncMock(return_value=inventory)), \
             patch("app.main.resolve_names", new=AsyncMock(return_value={10: "Rifter", 11: "Tritanium"})), \
             patch("app.main.ESIClient") as client:
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            response = self.client.get("/assets?q=trit&station=1000000001234")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Tritanium", response.text)
        self.assertNotIn("Rifter</td>", response.text)
        self.assertIn("1000000001234", response.text)
        self.assertIn("Standortname oder Zugriff fehlt", response.text)

    def test_filter_form_accepts_empty_selections_and_validates_ids(self):
        inventory = [
            {"item_id": 1, "type_id": 10, "quantity": 3, "location_id": 60003760, "location_type": "station"},
            {"item_id": 2, "type_id": 11, "quantity": 2, "location_id": 60000001, "location_type": "station"},
        ]
        with patch("app.main.paginated_list", new=AsyncMock(return_value=inventory)), \
             patch("app.main.resolve_names", new=AsyncMock(return_value={10: "Rifter", 11: "Tritanium"})), \
             patch("app.main.ESIClient") as client:
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            for query in ("?q=Rifter&station=&market=", "?q=&station=60003760&market=",
                          "?q=&station=&market=2", "?q=&station=60003760&market=2"):
                with self.subTest(query=query):
                    response = self.client.get("/assets" + query)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("Rifter", response.text)
            response = self.client.get("/assets?q=&station=60003760&market=")
            self.assertNotIn("Tritanium", response.text)
            response = self.client.get("/assets?q=&station=&market=2")
            self.assertIn('value="2" selected', response.text)
            for query in ("?station=abc", "?station=0", "?market=abc", "?market=-1"):
                with self.subTest(query=query):
                    self.assertEqual(self.client.get("/assets" + query).status_code, 422)

    def test_prices_are_per_location_and_no_order_is_not_zero(self):
        async def quote(location, type_ids):
            return {
                type_id: {"lowest_sell": 10 if location["id"] == 1 else None, "error": None}
                for type_id in type_ids
            }

        with patch("app.main.get_sell_prices", new=AsyncMock(side_effect=quote)):
            response = self.client.get("/assets/prices?type_ids=10,10&market=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["10"]["jita"]["lowest_sell"], 10)
        self.assertIsNone(response.json()["10"]["home"]["lowest_sell"])
        self.assertEqual(self.client.get("/assets/prices?type_ids=10&market=99").status_code, 400)
        self.assertEqual(self.client.get("/assets/prices?type_ids=" + ",".join(map(str, range(1, 22)))).status_code, 400)

    def test_missing_scope_and_character_switch(self):
        with patch("app.main.storage.get_character", return_value={"scopes": []}):
            response = self.client.get("/assets")
        self.assertIn("esi-assets.read_assets.v1", response.text)
        with patch("app.main.storage.character_belongs_to", return_value=True):
            response = self.client.post("/auth/chars/select", data={"character_id": 123, "next": "/assets"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/assets")

    def test_pagination_and_access_control(self):
        inventory = [
            {"item_id": i, "type_id": 10, "quantity": 1, "location_id": 60003760, "location_type": "station"}
            for i in range(1, 23)
        ]
        with patch("app.main.paginated_list", new=AsyncMock(return_value=inventory)), \
             patch("app.main.resolve_names", new=AsyncMock(return_value={10: "Rifter"})), \
             patch("app.main.ESIClient") as client:
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            response = self.client.get("/assets?page=2")
        self.assertIn("Seite 2 von 2", response.text)
        self.assertEqual(response.text.count('class="asset-row asset-price-row"'), 2)
        with patch("app.main.require_module", side_effect=main.HTTPException(404)):
            self.assertEqual(self.client.get("/assets").status_code, 404)
            self.assertEqual(self.client.get("/assets/prices?type_ids=10").status_code, 404)

    def test_bpc_not_priceable_even_when_same_type_has_bpo(self):
        inventory = [
            {"item_id": 1, "type_id": 10, "quantity": 1, "location_id": 60003760, "location_type": "station", "is_blueprint_copy": True},
            {"item_id": 2, "type_id": 10, "quantity": 1, "location_id": 60003760, "location_type": "station"},
            {"item_id": 3, "type_id": 11, "quantity": 2, "location_id": 60003760, "location_type": "station", "is_blueprint_copy": True},
            {"item_id": 4, "type_id": 12, "quantity": 5, "location_id": 60003760, "location_type": "station"},
        ]
        with patch("app.main.paginated_list", new=AsyncMock(return_value=inventory)), \
             patch("app.main.resolve_names", new=AsyncMock(return_value={10: "Rifter Blueprint", 11: "Other Blueprint", 12: "Tritanium"})), \
             patch("app.main.blueprint_types", new=AsyncMock(return_value={10: True, 12: False})) as categories, \
             patch("app.main.ESIClient") as client:
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            response = self.client.get("/assets?market=2")
        self.assertEqual(response.status_code, 200)
        categories.assert_awaited_once()
        self.assertEqual(categories.await_args.args[1], {10, 12})
        self.assertEqual(response.text.count("BPC · Kopie"), 2)
        self.assertEqual(response.text.count("BPO · Original"), 1)
        self.assertEqual(response.text.count("Kein Marktpreis für BPC"), 4)
        self.assertEqual(response.text.count('class="asset-row asset-price-row"'), 2)


class BlueprintLibraryRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.patches = [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
            patch("app.main.storage.get_character", return_value={"scopes": ["esi-assets.read_assets.v1"]}),
            patch("app.main._character_assets", new=AsyncMock(return_value=[{"item_id": 11, "type_id": 1}])),
            patch("app.main.load_sde", new=AsyncMock(return_value={
                "invTypes": [{"typeID": "1", "typeName": "Acolyte Blueprint", "groupID": "10"}],
                "invGroups": [{"groupID": "10", "groupName": "Drone Blueprints", "categoryID": "9"}],
            })),
            patch("app.main.storage.sync_blueprint_library"),
            patch("app.main.storage.blueprint_library", return_value=[{
                "item_id": 11, "type_id": 1, "type_name": "Acolyte Blueprint",
                "group_name": "Drone Blueprints", "is_copy": 0, "runs": None,
                "material_efficiency": 10, "time_efficiency": 20, "is_shared": 0,
            }]),
            patch("app.main.storage.shared_blueprint_library", return_value=[{
                "item_id": 22, "type_id": 1, "type_name": "Acolyte Blueprint",
                "group_name": "Drone Blueprints", "is_copy": 1, "runs": 3,
                "material_efficiency": 8, "time_efficiency": 16, "is_shared": 1,
                "account_id": 2, "character_name": "Other Pilot", "synced_at": "2026-11-08",
            }]),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_page_lists_own_bpo_and_other_players_shared_bpc(self):
        response = self.client.get("/bpo-bib?q=Acolyte&group=Drone%20Blueprints")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Acolyte Blueprint", response.text)
        self.assertIn("Drone Blueprints", response.text)
        self.assertIn("10", response.text)
        self.assertIn("Other Pilot", response.text)
        self.assertIn("3", response.text)

    def test_share_and_unshare_require_owned_blueprint(self):
        with patch("app.main.storage.set_blueprint_shared", return_value=True) as update:
            response = self.client.post(
                "/bpo-bib/share",
                data={"item_id": 11, "action": "share"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        update.assert_called_once_with(123, 11, True)
        with patch("app.main.storage.set_blueprint_shared") as update:
            response = self.client.post(
                "/bpo-bib/share",
                data={"item_id": 99, "action": "share"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 404)
        update.assert_not_called()

    def test_invalid_share_action_rejected(self):
        response = self.client.post(
            "/bpo-bib/share",
            data={"item_id": 11, "action": "delete"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400)


class MarketPricesTests(unittest.TestCase):
    def test_structure_order_book_is_read_once(self):
        orders = [
            {"type_id": 10, "is_buy_order": False, "price": 15},
            {"type_id": 10, "is_buy_order": False, "price": 12},
            {"type_id": 11, "is_buy_order": True, "price": 42},
        ]
        with patch("app.services.market.storage.get_character", return_value={"scopes": ["esi-markets.structure_markets.v1"]}), \
             patch("app.services.market.ESIClient") as client, \
             patch("app.services.market._paginated_orders", new=AsyncMock(return_value=orders)) as fetch:
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            result = asyncio.run(sell_prices({"kind": "structure", "location_id": 1000000001234, "character_id": 1}, [10, 11]))
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(result[10]["lowest_sell"], 12)
        self.assertIsNone(result[11]["lowest_sell"])

    def test_station_uses_only_sell_orders_at_selected_station(self):
        orders = [
            {"type_id": 10, "location_id": 1, "is_buy_order": False, "price": 15},
            {"type_id": 10, "location_id": 2, "is_buy_order": False, "price": 3},
            {"type_id": 10, "location_id": 1, "is_buy_order": True, "price": 1},
        ]
        with patch("app.services.market.ESIClient") as client, \
             patch("app.services.market._paginated_orders", new=AsyncMock(return_value=orders)):
            client.return_value.__aenter__ = AsyncMock(return_value=client)
            client.return_value.__aexit__ = AsyncMock(return_value=None)
            result = asyncio.run(sell_prices({"kind": "station", "location_id": 1, "region_id": 7}, [10]))
        self.assertEqual(result[10]["lowest_sell"], 15)


if __name__ == "__main__":
    unittest.main()
