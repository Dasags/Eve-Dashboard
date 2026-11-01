import math
from collections import defaultdict

from app.services.sde import as_float, as_int, field

INDUSTRY_SKILL_ID = 3380
ADVANCED_INDUSTRY_SKILL_ID = 3388
REACTIONS_SKILL_ID = 45746


def skill_time_factor(activity_id: int, levels: dict[int, int]) -> float:
    if activity_id == 9:
        return 1 - 0.04 * levels.get(REACTIONS_SKILL_ID, 0)
    return (1 - 0.04 * levels.get(INDUSTRY_SKILL_ID, 0)) * (
        1 - 0.03 * levels.get(ADVANCED_INDUSTRY_SKILL_ID, 0)
    )


def create_plan(
    product_id: int,
    runs: int,
    recipes: dict[int, list[dict]],
    types: dict[int, dict],
    group_names: dict[int, str],
    activity_id: int,
    build_components: bool,
    product_me: float,
    product_te: float,
    component_me: float,
    component_te: float,
    structure_material_bonus: float,
    structure_time_bonus: float,
    rig_material_bonus: float,
    rig_time_bonus: float,
    security_factor: float,
    system_cost_index: float,
    cost_bonus: float,
    tax_rate: float,
    adjusted_prices: dict[int, float],
    skill_time_factor: float = 1.0,
) -> dict:
    if product_id not in types:
        raise ValueError("Produkt ist nicht in der SDE vorhanden.")
    if runs < 1 or runs > 10000:
        raise ValueError("Läufe müssen zwischen 1 und 10.000 liegen.")
    purchases: dict[int, int] = defaultdict(int)
    steps: list[dict] = []
    total_eiv_inputs: dict[int, int] = defaultdict(int)
    sequential_seconds = 0.0
    visiting: set[int] = set()

    def expand(type_id: int, quantity: int, depth: int) -> float:
        nonlocal sequential_seconds
        candidates = [
            item for item in recipes.get(type_id, []) if item["activity_id"] == activity_id
        ]
        if not candidates or (depth > 0 and not build_components):
            purchases[type_id] += quantity
            return 0
        if type_id in visiting:
            raise ValueError("Zyklische Rezeptdaten in der SDE erkannt.")
        recipe = candidates[0]
        visiting.add(type_id)
        output = recipe["output_quantity"]
        recipe_runs = math.ceil(quantity / output)
        me = product_me if depth == 0 else component_me
        te = product_te if depth == 0 else component_te
        material_factor = (
            (1 - min(1, max(0, me / 100)))
            * (1 - min(1, max(0, structure_material_bonus / 100)))
            * (1 - min(1, max(0, rig_material_bonus * security_factor / 100)))
        )
        time_factor = (
            (1 - min(1, max(0, te / 100)))
            * (1 - min(1, max(0, structure_time_bonus / 100)))
            * (1 - min(1, max(0, rig_time_bonus * security_factor / 100)))
            * skill_time_factor
        )
        material_reduction = 1 - material_factor
        time_reduction = 1 - time_factor
        child_critical_seconds = 0.0
        step_materials = []
        for material in recipe["materials"]:
            material_quantity = max(
                1,
                math.ceil(
                    material["quantity"] * recipe_runs * (1 - material_reduction)
                ),
            )
            total_eiv_inputs[material["type_id"]] += material_quantity
            step_materials.append(
                {"type_id": material["type_id"], "quantity": material_quantity}
            )
            child_critical_seconds = max(
                child_critical_seconds,
                expand(material["type_id"], material_quantity, depth + 1),
            )
        duration = recipe["time_seconds"] * recipe_runs * (1 - time_reduction)
        sequential_seconds += duration
        steps.append(
            {
                "type_id": type_id,
                "blueprint_id": recipe["blueprint_id"],
                "name": field(types[type_id], "typeName", "type_name", default=str(type_id)),
                "activity": recipe["activity"],
                "runs": recipe_runs,
                "quantity": recipe_runs * output,
                "seconds": duration,
                "materials": step_materials,
                "depth": depth,
            }
        )
        visiting.remove(type_id)
        return child_critical_seconds + duration

    critical_seconds = expand(product_id, runs, 0)
    materials = []
    for type_id, quantity in sorted(
        purchases.items(),
        key=lambda item: field(types.get(item[0], {}), "typeName").casefold(),
    ):
        row = types.get(type_id, {})
        materials.append(
            {
                "type_id": type_id,
                "name": field(row, "typeName", "type_name", default=f"Item {type_id}"),
                "group_name": group_names.get(as_int(row, "groupID"), ""),
                "quantity": quantity,
            }
        )
    eiv_items = [
        {
            "type_id": type_id,
            "quantity": quantity,
            "adjusted_price": adjusted_prices.get(type_id),
        }
        for type_id, quantity in total_eiv_inputs.items()
    ]
    job_cost = None
    if all(item["adjusted_price"] is not None for item in eiv_items):
        job_cost = sum(
            item["adjusted_price"] * item["quantity"] for item in eiv_items
        ) * ((system_cost_index / 100) * (1 - cost_bonus / 100) + tax_rate / 100 + 0.04)
    product_recipe = next(
        item for item in recipes[product_id] if item["activity_id"] == activity_id
    )
    return {
        "product": field(types[product_id], "typeName", "type_name", default=str(product_id)),
        "product_id": product_id,
        "runs": runs,
        "output_quantity": runs * product_recipe["output_quantity"],
        "materials": materials,
        "steps": steps,
        "eiv_items": eiv_items,
        "material_cost": 0.0,
        "to_buy_cost": 0.0,
        "job_cost": job_cost,
        "total_cost": job_cost,
        "product_value": 0.0,
        "margin": None,
        "sequential_seconds": sequential_seconds,
        "critical_seconds": critical_seconds,
    }


def apply_market_and_stock(
    plan: dict,
    quotes: dict[int, dict],
    stock: dict[int, int],
    adjusted_prices: dict[int, float],
) -> dict:
    material_cost = 0.0
    to_buy_cost = 0.0
    missing_prices = []
    for item in plan["materials"]:
        quote = quotes.get(item["type_id"], {})
        unit_price = quote.get("lowest_sell")
        if unit_price is None:
            unit_price = adjusted_prices.get(item["type_id"])
        item["unit_price"] = unit_price
        item["stock"] = max(0, stock.get(item["type_id"], 0))
        item["to_buy"] = max(0, item["quantity"] - item["stock"])
        if unit_price is None:
            item["total_cost"] = None
            item["to_buy_cost"] = None
            missing_prices.append(item["name"])
        else:
            item["total_cost"] = item["quantity"] * unit_price
            item["to_buy_cost"] = item["to_buy"] * unit_price
            material_cost += item["total_cost"]
            to_buy_cost += item["to_buy_cost"]
    product_quote = quotes.get(plan["product_id"], {})
    product_unit_price = product_quote.get("lowest_sell")
    if product_unit_price is None:
        product_unit_price = adjusted_prices.get(plan["product_id"])
    plan["product_unit_price"] = product_unit_price
    plan["material_cost"] = None if missing_prices else material_cost
    plan["to_buy_cost"] = None if missing_prices else to_buy_cost
    plan["total_cost"] = (
        material_cost + plan["job_cost"]
        if not missing_prices and plan["job_cost"] is not None
        else None
    )
    plan["product_value"] = (
        product_unit_price * plan["output_quantity"] if product_unit_price is not None else None
    )
    plan["margin"] = (
        plan["product_value"] - plan["total_cost"]
        if plan["product_value"] is not None and plan["total_cost"] is not None
        else None
    )
    plan["missing_prices"] = missing_prices
    plan["missing_adjusted_prices"] = [
        item["type_id"] for item in plan["eiv_items"] if item["adjusted_price"] is None
    ]
    plan["price_source"] = "Jita-Verkaufspreis, Fallback ESI-Adjusted-Preis"
    return plan
