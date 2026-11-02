import math
from collections import defaultdict

from app.services.sde import as_int, field

PLANET_RESOURCES = {
    "Barren": ["Aqueous Liquids", "Base Metals", "Carbon Compounds", "Micro Organisms", "Noble Metals"],
    "Gas": ["Aqueous Liquids", "Base Metals", "Ionic Solutions", "Noble Gas", "Reactive Gas"],
    "Ice": ["Aqueous Liquids", "Heavy Metals", "Micro Organisms", "Noble Gas", "Planktic Colonies"],
    "Lava": ["Base Metals", "Felsic Magma", "Heavy Metals", "Non-CS Crystals", "Suspended Plasma"],
    "Oceanic": ["Aqueous Liquids", "Carbon Compounds", "Complex Organisms", "Micro Organisms", "Planktic Colonies"],
    "Plasma": ["Base Metals", "Heavy Metals", "Noble Metals", "Non-CS Crystals", "Suspended Plasma"],
    "Storm": ["Aqueous Liquids", "Base Metals", "Ionic Solutions", "Noble Gas", "Suspended Plasma"],
    "Temperate": ["Aqueous Liquids", "Autotrophs", "Carbon Compounds", "Complex Organisms", "Micro Organisms"],
}


def create_plan(
    product_id: int,
    quantity: int,
    buy_level: int,
    recipes: dict[int, dict],
    types: dict[int, dict],
    group_names: dict[int, str],
) -> dict:
    if product_id not in types:
        raise ValueError("PI-Produkt ist nicht in der SDE vorhanden.")
    if not 1 <= quantity <= 1_000_000 or not 0 <= buy_level <= 3:
        raise ValueError("Produktmenge oder Einkaufsstufe ist ungültig.")
    purchases: dict[int, int] = defaultdict(int)
    tree: list[dict] = []
    steps: list[dict] = []
    sequential_seconds = 0
    visiting: set[int] = set()

    tier_cache: dict[int, int] = {}

    def structural_tier(type_id: int, trail: frozenset = frozenset()) -> int:
        if type_id in tier_cache:
            return tier_cache[type_id]
        recipe = recipes.get(type_id)
        if recipe is None or type_id in trail or not recipe["materials"]:
            return 0
        tier = 1 + max(
            structural_tier(item["type_id"], trail | {type_id})
            for item in recipe["materials"]
        )
        tier_cache[type_id] = tier
        return tier

    def item_info(type_id: int) -> tuple[str, int | None, str]:
        row = types.get(type_id, {})
        name = field(row, "typeName", "type_name", default=f"Item {type_id}")
        group_name = group_names.get(as_int(row, "groupID"), "")
        return name, structural_tier(type_id), group_name

    def expand(type_id: int, needed: int, depth: int, is_root: bool = False) -> int:
        nonlocal sequential_seconds
        name, tier, group_name = item_info(type_id)
        recipe = recipes.get(type_id)
        buy_material = (
            recipe is None
            or (not is_root and (tier is None or tier <= buy_level))
        )
        if buy_material:
            purchases[type_id] += needed
            tree.append(
                {
                    "type_id": type_id,
                    "name": name,
                    "tier": tier,
                    "group": group_name,
                    "quantity": needed,
                    "kind": "Kaufen",
                    "depth": depth,
                    "cycles": 0,
                    "seconds": 0,
                }
            )
            return 0
        if type_id in visiting:
            raise ValueError("Zyklische Planetary-Industry-Rezeptdaten erkannt.")
        visiting.add(type_id)
        cycles = math.ceil(needed / recipe["output_quantity"])
        produced = cycles * recipe["output_quantity"]
        row = {
            "type_id": type_id,
            "name": name,
            "tier": tier,
            "group": group_name,
            "quantity": produced,
            "kind": "Herstellen",
            "depth": depth,
            "cycles": cycles,
            "seconds": cycles * recipe["cycle_seconds"],
        }
        tree.append(row)
        child_critical_seconds = 0
        for material in recipe["materials"]:
            child_critical_seconds = max(
                child_critical_seconds,
                expand(
                    material["type_id"],
                    material["quantity"] * cycles,
                    depth + 1,
                ),
            )
        duration = cycles * recipe["cycle_seconds"]
        sequential_seconds += duration
        steps.append(row)
        visiting.remove(type_id)
        return child_critical_seconds + duration

    critical_seconds = expand(product_id, quantity, 0, True)
    output_quantity = next(row["quantity"] for row in tree if row["depth"] == 0)
    materials = []
    for type_id, needed in purchases.items():
        name, tier, group_name = item_info(type_id)
        materials.append(
            {
                "type_id": type_id,
                "name": name,
                "tier": tier,
                "group": group_name,
                "quantity": needed,
            }
        )
    materials.sort(key=lambda item: (item["tier"] is None, item["tier"] or 0, item["name"]))
    tree.sort(key=lambda item: (item["depth"], item["name"]))
    steps.sort(key=lambda item: item["depth"])
    return {
        "product": item_info(product_id)[0],
        "product_id": product_id,
        "requested_quantity": quantity,
        "output_quantity": output_quantity,
        "buy_level": buy_level,
        "tree": tree,
        "steps": steps,
        "materials": materials,
        "sequential_seconds": sequential_seconds,
        "critical_seconds": critical_seconds,
    }


def add_location_prices(plan: dict, quotes: dict[int, dict]) -> dict:
    missing = [
        item["name"]
        for item in plan["materials"]
        if quotes.get(item["type_id"], {}).get("lowest_sell") is None
    ]
    cost = (
        sum(
            quotes[item["type_id"]]["lowest_sell"] * item["quantity"]
            for item in plan["materials"]
        )
        if not missing
        else None
    )
    product_quote = quotes.get(plan["product_id"], {})
    price = product_quote.get("lowest_sell")
    return {
        "cost": cost,
        "product_price": price,
        "product_value": price * plan["output_quantity"] if price is not None else None,
        "margin": (
            price * plan["output_quantity"] - cost
            if price is not None and cost is not None
            else None
        ),
        "missing_prices": missing,
    }


def analyze(plan: dict, locations: list[dict], recipes: dict, raw_plan: dict) -> dict:
    """Aggregate per-station results into best-station, tier and item views."""
    for entry in locations:
        summary = entry["summary"]
        cost, margin = summary["cost"], summary["margin"]
        summary["margin_pct"] = margin / cost * 100 if margin is not None and cost else None
    ranked = [entry for entry in locations if entry["summary"]["margin"] is not None]
    best = max(ranked, key=lambda entry: entry["summary"]["margin"]) if ranked else None
    best_ids = {id(best)} if best else set()

    def price_row(row: dict) -> dict:
        prices = []
        for entry in locations:
            price = entry["quotes"].get(row["type_id"], {}).get("lowest_sell")
            prices.append({"location": entry["location"], "price": price})
        priced = [item for item in prices if item["price"] is not None]
        cheapest = min(priced, key=lambda item: item["price"]) if priced else None
        priciest = max(priced, key=lambda item: item["price"]) if priced else None
        return {
            **row,
            "prices": prices,
            "cheapest": cheapest,
            "spread_pct": (
                (priciest["price"] - cheapest["price"]) / cheapest["price"] * 100
                if cheapest and cheapest["price"]
                else None
            ),
        }

    items = [price_row(row) for row in plan["tree"]]
    tiers: dict[int | None, dict] = {}
    for item in items:
        group = tiers.setdefault(
            item["tier"],
            {"tier": item["tier"], "count": 0, "quantity": 0, "buy_cost": 0.0, "complete": True, "seconds": 0},
        )
        group["count"] += 1
        group["quantity"] += item["quantity"]
        group["seconds"] += item["seconds"]
        if item["kind"] == "Kaufen":
            if item["cheapest"]:
                group["buy_cost"] += item["cheapest"]["price"] * item["quantity"]
            else:
                group["complete"] = False
    tier_rows = sorted(tiers.values(), key=lambda row: (row["tier"] is None, row["tier"] or 0))
    basis = (best or (locations[0] if locations else {"quotes": {}}))["quotes"]

    def unit_price(type_id: int) -> float | None:
        return basis.get(type_id, {}).get("lowest_sell")

    merged: dict[int, dict] = {}
    for row in plan["tree"]:
        current = merged.setdefault(row["type_id"], {**row, "quantity": 0, "cycles": 0})
        current["quantity"] += row["quantity"]
        current["cycles"] += row["cycles"]
    tier_groups: dict[int, list[dict]] = {}
    for row in merged.values():
        price = unit_price(row["type_id"])
        sell = price * row["quantity"] if price is not None else None
        cost = None
        if row["kind"] == "Herstellen":
            parts = [
                (unit_price(m["type_id"]), m["quantity"] * row["cycles"])
                for m in recipes[row["type_id"]]["materials"]
            ]
            if all(unit is not None for unit, _ in parts):
                cost = sum(unit * qty for unit, qty in parts)
        profit = sell - cost if sell is not None and cost is not None else None
        tier_groups.setdefault(row["tier"] if row["tier"] is not None else -1, []).append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "quantity": row["quantity"],
                "cycles": row["cycles"],
                "seconds": row["seconds"],
                "sell": sell,
                "cost": cost,
                "profit": profit,
                "margin": profit / cost * 100 if profit is not None and cost else None,
            }
        )
    groups = [
        {"tier": tier, "rows": sorted(rows, key=lambda r: r["name"])}
        for tier, rows in sorted(tier_groups.items(), reverse=True)
    ]

    needs = {
        item["name"]: item["quantity"]
        for item in raw_plan["materials"]
        if item["tier"] == 0
    }
    lookup = {name.casefold(): name for name in needs}
    coverage = {
        planet: [lookup[res.casefold()] for res in resources if res.casefold() in lookup]
        for planet, resources in PLANET_RESOURCES.items()
    }
    ranking = sorted(
        ({"planet": p, "covers": c} for p, c in coverage.items() if c),
        key=lambda row: (-len(row["covers"]), row["planet"]),
    )
    remaining, setup = set(needs), []
    while remaining:
        pick = max(coverage, key=lambda p: (len(remaining & set(coverage[p])), p))
        gained = sorted(remaining & set(coverage[pick]))
        if not gained:
            break
        setup.append({"planet": pick, "resources": gained})
        remaining -= set(gained)
    planets = {
        "needs": [
            {
                "name": name,
                "quantity": qty,
                "planets": [p for p, c in coverage.items() if name in c],
            }
            for name, qty in sorted(needs.items())
        ],
        "ranking": ranking,
        "setup": setup,
    }
    return {
        "tier_groups": groups,
        "planets": planets,
        "best": best,
        "best_ids": best_ids,
        "items": items,
        "tiers": tier_rows,
        "cheapest_total": (
            sum(
                row["cheapest"]["price"] * row["quantity"]
                for row in items
                if row["kind"] == "Kaufen" and row["cheapest"]
            )
            if all(row["cheapest"] for row in items if row["kind"] == "Kaufen")
            else None
        ),
    }
