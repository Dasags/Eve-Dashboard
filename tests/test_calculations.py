import atexit
import os
import shutil
import tempfile
import unittest

if "DATA_DIR" not in os.environ:
    _test_data_dir = tempfile.mkdtemp(prefix="eve-dashboard-tests-")
    os.environ["DATA_DIR"] = _test_data_dir
    atexit.register(shutil.rmtree, _test_data_dir, ignore_errors=True)
os.environ.setdefault("SESSION_SECRET", "test-session-secret")

from app.services.planetary import add_location_prices
from app.services.planetary import create_plan as create_planetary
from app.services.production import apply_market_and_stock, create_plan


class ProductionCalculationTests(unittest.TestCase):
    def setUp(self):
        self.types = {
            100: {"typeName": "Test Ship", "groupID": "1"},
            200: {"typeName": "Test Component", "groupID": "1"},
            300: {"typeName": "Test Mineral", "groupID": "2"},
            400: {"typeName": "Test Gas", "groupID": "2"},
        }
        self.recipes = {
            100: [
                {
                    "blueprint_id": 1000,
                    "activity_id": 1,
                    "activity": "Herstellung",
                    "output_quantity": 1,
                    "time_seconds": 60,
                    "materials": [{"type_id": 200, "quantity": 2}],
                }
            ],
            200: [
                {
                    "blueprint_id": 2000,
                    "activity_id": 1,
                    "activity": "Herstellung",
                    "output_quantity": 1,
                    "time_seconds": 30,
                    "materials": [{"type_id": 400, "quantity": 10}],
                }
            ],
        }

    def test_components_expand_into_purchases_and_stock(self):
        plan = create_plan(
            100,
            2,
            self.recipes,
            self.types,
            {},
            1,
            True,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.5,
            0,
            0,
            0.25,
            {200: 5, 400: 3},
        )
        self.assertEqual(plan["output_quantity"], 2)
        self.assertEqual(plan["sequential_seconds"], 240)
        self.assertEqual(plan["critical_seconds"], 240)
        self.assertEqual(
            {item["type_id"]: item["quantity"] for item in plan["materials"]},
            {400: 40},
        )

        result = apply_market_and_stock(
            plan,
            {
                100: {"lowest_sell": 100},
                400: {"lowest_sell": 3},
            },
            {400: 10},
            {400: 3},
        )
        self.assertEqual(result["material_cost"], 120)
        self.assertEqual(result["to_buy_cost"], 90)
        self.assertEqual(result["product_value"], 200)

    def test_missing_prices_remain_unpriced(self):
        plan = create_plan(
            100,
            1,
            self.recipes,
            self.types,
            {},
            1,
            False,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0.5,
            0,
            0,
            0.25,
            {},
        )
        result = apply_market_and_stock(plan, {}, {}, {})
        self.assertIsNone(result["material_cost"])
        self.assertIsNone(result["total_cost"])
        self.assertEqual(result["missing_prices"], ["Test Component"])


class PlanetaryCalculationTests(unittest.TestCase):
    def test_cycle_rounding_and_margin_use_actual_output(self):
        types = {
            100: {"typeName": "Test Robotics", "groupID": "4"},
            200: {"typeName": "Test Coolant", "groupID": "3"},
            300: {"typeName": "Test Water", "groupID": "2"},
        }
        groups = {
            2: "Basic Commodities",
            3: "Advanced Commodities",
            4: "Advanced Industry",
        }
        recipes = {
            100: {
                "output_quantity": 3,
                "cycle_seconds": 100,
                "materials": [{"type_id": 200, "quantity": 2}],
            },
            200: {
                "output_quantity": 2,
                "cycle_seconds": 50,
                "materials": [{"type_id": 300, "quantity": 3}],
            },
        }

        plan = create_planetary(100, 4, 0, recipes, types, groups)
        self.assertEqual(plan["output_quantity"], 6)
        self.assertEqual(plan["sequential_seconds"], 300)
        self.assertEqual(plan["materials"][0]["quantity"], 6)

        summary = add_location_prices(
            plan,
            {
                100: {"lowest_sell": 100},
                300: {"lowest_sell": 10},
            },
        )
        self.assertEqual(summary["cost"], 60)
        self.assertEqual(summary["product_value"], 600)
        self.assertEqual(summary["margin"], 540)


if __name__ == "__main__":
    unittest.main()
