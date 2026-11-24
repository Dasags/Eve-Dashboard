import unittest
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main
from app.services.refinery import apply_prices, calculate, parse_inventory
from app.services import sde


TYPES = [
    {"typeID": "1", "typeName": "Veldspar", "portionSize": "100"},
    {"typeID": "2", "typeName": "Compressed Blue Ice", "portionSize": "100"},
    {"typeID": "3", "typeName": "Tritanium", "portionSize": "1"},
    {"typeID": "4", "typeName": "Heavy Water", "portionSize": "1"},
]
MATERIALS = [
    {"typeID": "1", "materialTypeID": "3", "quantity": "400"},
    {"typeID": "2", "materialTypeID": "4", "quantity": "25"},
]


class RefineryCalculationTests(unittest.TestCase):
    def test_paste_batches_duplicates_ice_and_remainder(self):
        entries, errors = parse_inventory(
            "Type\tQuantity\nVeldspar\t1,050\tOre\nveldSPAR\t50\tOre\nCompressed Blue Ice\t250"
        )
        self.assertEqual(errors, [])
        result = calculate(entries, TYPES, MATERIALS, Decimal("70"))
        self.assertEqual([(x["processed"], x["remainder"]) for x in result["inputs"]], [(1100, 0), (200, 50)])
        self.assertEqual({x["name"]: x["quantity"] for x in result["outputs"]}, {"Tritanium": 3080, "Heavy Water": 35})

    def test_client_asterisk_and_distinct_errors(self):
        entries, errors = parse_inventory("Veldspar*\t200\nVELDSPAR *\t100\nTritanium\t5\nNope\t1")
        self.assertEqual(errors, [])
        self.assertEqual(entries["veldspar"], 300)
        result = calculate(entries, TYPES, MATERIALS, Decimal("50"))
        self.assertEqual(result["outputs"][0]["quantity"], 600)
        self.assertIn("nicht wiederaufbereitbar", result["errors"][0].lower())
        self.assertIn("nicht in der SDE", result["errors"][1])

    def test_rounding_zero_lots_unknown_and_invalid(self):
        entries, errors = parse_inventory("Veldspar\t101\nUnknown\t4\nbad input\nVeldspar\t0")
        self.assertEqual(len(errors), 2)
        result = calculate(entries, TYPES, MATERIALS, Decimal("33.333"))
        self.assertEqual(result["outputs"][0]["quantity"], 133)
        self.assertEqual(result["inputs"][0]["remainder"], 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_price_completeness_and_leftovers(self):
        entries, _ = parse_inventory("Veldspar\t150")
        result = calculate(entries, TYPES, MATERIALS, Decimal("50"))
        apply_prices(result, {1: {"highest_buy": 2, "lowest_sell": 3}, 3: {"highest_buy": 4}})
        self.assertEqual(result["inputs_highest_buy"], 200)
        self.assertEqual(result["outputs_highest_buy"], 800)
        self.assertIsNone(result["outputs_lowest_sell"])
        result["errors"].append("Unrecognized")
        apply_prices(result, {1: {"highest_buy": 2}, 3: {"highest_buy": 4}})
        self.assertIsNone(result["outputs_highest_buy"])

    def test_zero_yield_and_fractional_lot(self):
        result = calculate({"veldspar": 99}, TYPES, MATERIALS, Decimal("70"))
        self.assertEqual(result["inputs"][0]["remainder"], 99)
        self.assertEqual(result["outputs"], [])
        apply_prices(result, {})
        self.assertIsNone(result["inputs_highest_buy"])
        result = calculate({"veldspar": 100}, TYPES, MATERIALS, Decimal("0"))
        apply_prices(result, {1: {"highest_buy": 2}})
        self.assertEqual(result["outputs_highest_buy"], 0)


class RefineryPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        for item in [
            patch("app.main.require_account", return_value=(1, 123)),
            patch("app.main.require_module"),
            patch("app.main.current_account", return_value=1),
            patch("app.main.storage.list_characters", return_value=[]),
            patch("app.main.storage.list_modules", return_value=[]),
            patch("app.main.storage.account_module_names", return_value=set()),
        ]:
            item.start()
            self.addCleanup(item.stop)

    def test_page_and_calculation_render(self):
        self.assertIn("REFINERY-TERMINAL // EINGABE", self.client.get("/refinery").text)
        prices = {1: {"highest_buy": 2, "lowest_sell": 3}, 3: {"highest_buy": 4, "lowest_sell": 5}}
        with (
            patch("app.main.load_sde", new=AsyncMock(return_value={"invTypes": TYPES})),
            patch("app.main.load_reprocessing_materials", new=AsyncMock(return_value=MATERIALS)),
            patch("app.main.station_order_prices", new=AsyncMock(return_value=prices)) as quote,
        ):
            response = self.client.post("/refinery/calculate", data={"inventory": "Veldspar\t150", "yield_percent": "50"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Tritanium", response.text)
        self.assertIn("800,00 ISK", response.text)
        self.assertIn("<td>50</td>", response.text)
        self.assertEqual(quote.call_args.args[0]["location_id"], 60003760)

    def test_partial_input_and_market_failure_preserve_results(self):
        with (
            patch("app.main.load_sde", new=AsyncMock(return_value={"invTypes": TYPES})),
            patch("app.main.load_reprocessing_materials", new=AsyncMock(return_value=MATERIALS)),
            patch("app.main.station_order_prices", new=AsyncMock(side_effect=RuntimeError("Markt nicht erreichbar"))),
        ):
            response = self.client.post("/refinery/calculate", data={"inventory": "Veldspar\t100\nbroken", "yield_percent": "70"})
        self.assertIn("Tritanium", response.text)
        self.assertIn("Markt nicht erreichbar", response.text)
        self.assertIn("Unvollständige Eingabe", response.text)
        self.assertIn("Nicht vollständig", response.text)

    def test_invalid_yield_rejected(self):
        for value in ("NaN", "101", "-1", "abc", "9" * 30):
            response = self.client.post("/refinery/calculate", data={"inventory": "Veldspar\t100", "yield_percent": value})
            self.assertIn('role="alert"', response.text)


class StationPricesTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_station_orders_and_missing_prices(self):
        esi = AsyncMock()
        esi.__aenter__.return_value = esi
        orders = [
            {"type_id": 1, "location_id": 60003760, "is_buy_order": True, "price": 4},
            {"type_id": 1, "location_id": 60003760, "is_buy_order": False, "price": 8},
            {"type_id": 1, "location_id": 9, "is_buy_order": True, "price": 100},
        ]
        with patch("app.services.market.ESIClient", return_value=esi), patch(
            "app.services.market._paginated_orders", new=AsyncMock(return_value=orders)
        ):
            result = await main.station_order_prices({"kind": "station", "region_id": 10000002, "location_id": 60003760}, [1, 2])
        self.assertEqual(result[1]["highest_buy"], 4)
        self.assertEqual(result[1]["lowest_sell"], 8)
        self.assertIsNone(result[2]["highest_buy"])


class RefinerySDETests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_cache_survives_invalid_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invTypeMaterials.csv"
            original = "typeID,materialTypeID,quantity\n1,3,400\n"
            path.write_text(original)
            os.utime(path, (1, 1))
            client = AsyncMock()
            client.__aenter__.return_value = client
            response = unittest.mock.Mock()
            response.text = "invalid,data\nfoo,bar"
            client.get.return_value = response
            with (
                patch.object(sde, "CACHE_DIR", Path(directory)),
                patch.object(sde, "_materials_cache", None),
                patch.object(sde, "_materials_cached_at", 0.0),
                patch("app.services.sde.httpx.AsyncClient", return_value=client),
            ):
                rows = await sde.load_reprocessing_materials()
            self.assertEqual(rows[0]["quantity"], "400")
            self.assertEqual(path.read_text(), original)
