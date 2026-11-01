import unittest

from app.services.invention import apply_invention, plan_invention
from app.services.production import skill_time_factor

TYPES = {
    1: {"typeName": "Gallentean Cybernetics"},
    2: {"typeName": "Hydromagnetic Physics"},
    3: {"typeName": "Gallentean Starship Encryption Methods"},
}
RECIPE = {
    "source_blueprint_id": 10,
    "base_probability": 0.3,
    "output_runs": 1,
    "time_seconds": 1000,
    "materials": [{"type_id": 100, "quantity": 2}],
    "skills": [
        {"skill_id": 1, "level": 3},
        {"skill_id": 2, "level": 3},
        {"skill_id": 3, "level": 1},
    ],
}


class InventionTests(unittest.TestCase):
    def test_skill_time_factor(self):
        self.assertAlmostEqual(skill_time_factor(1, {3380: 5, 3388: 5}), 0.8 * 0.85)
        self.assertAlmostEqual(skill_time_factor(9, {45746: 5}), 0.8)
        self.assertEqual(skill_time_factor(1, {}), 1.0)

    def test_probability_and_missing(self):
        plan = plan_invention(RECIPE, {1: 5, 2: 5, 3: 5}, "", TYPES)
        self.assertAlmostEqual(plan["probability"], 0.3 * (1 + 10 / 30 + 5 / 40))
        self.assertEqual(plan["missing_skills"], [])
        self.assertTrue(plan_invention(RECIPE, {}, "", TYPES)["missing_skills"])
        dec = plan_invention(RECIPE, {1: 5, 2: 5, 3: 5}, "Process Decryptor", TYPES)
        self.assertEqual((dec["me"], dec["te"]), (5, 10))

    def test_costs(self):
        inv = plan_invention(RECIPE, {1: 5, 2: 5, 3: 5}, "", TYPES)
        plan = {"runs": 10, "total_cost": 1000.0, "product_value": 5000.0, "margin": 4000.0}
        out = apply_invention(
            plan, inv, {100: "Datacore"}, {100: {"lowest_sell": 10.0}}, {200: 5.0},
            [{"type_id": 200, "quantity": 10}], None, 2.0, 0.0, 0.0,
        )
        self.assertEqual(inv["blueprint_copies"], 10)
        self.assertAlmostEqual(inv["attempts"], 10 / inv["probability"])
        self.assertGreater(out["total_cost"], 1000.0)
        self.assertAlmostEqual(out["margin"], 5000.0 - out["total_cost"])


if __name__ == "__main__":
    unittest.main()
