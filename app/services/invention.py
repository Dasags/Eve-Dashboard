import math

from app.services.sde import field

# Name: (Erfolgswahrscheinlichkeit %, Lauf-Modifikator, ME-Modifikator, TE-Modifikator)
DECRYPTORS = {
    "Accelerant Decryptor": (20, 1, 2, 10),
    "Attainment Decryptor": (80, 4, -1, 4),
    "Augmentation Decryptor": (-40, 9, -2, 2),
    "Optimized Attainment Decryptor": (90, 2, 1, -2),
    "Optimized Augmentation Decryptor": (-10, 7, 2, 0),
    "Parity Decryptor": (50, 3, 1, -2),
    "Process Decryptor": (10, 0, 3, 6),
    "Symmetry Decryptor": (0, 2, 1, 8),
}
BASE_ME = 2
BASE_TE = 4
JOB_COST_FACTOR = 0.02


def plan_invention(
    recipe: dict,
    levels: dict[int, int],
    decryptor_name: str,
    types: dict[int, dict],
) -> dict:
    if decryptor_name and decryptor_name not in DECRYPTORS:
        raise ValueError("Unbekannter Decryptor.")
    probability_mod, runs_mod, me_mod, te_mod = DECRYPTORS.get(decryptor_name, (0, 0, 0, 0))
    science, encryption, missing = [], [], []
    for skill in recipe["skills"]:
        name = field(types.get(skill["skill_id"], {}), "typeName", default=str(skill["skill_id"]))
        level = levels.get(skill["skill_id"], 0)
        entry = {"name": name, "level": level, "required": skill["level"]}
        (encryption if "encryption methods" in name.casefold() else science).append(entry)
        if level < skill["level"]:
            missing.append(f"{name} {skill['level']}")
    probability = min(
        1.0,
        recipe["base_probability"]
        * (
            1
            + sum(item["level"] for item in science) / 30
            + sum(item["level"] for item in encryption) / 40
        )
        * (1 + probability_mod / 100),
    )
    return {
        "source_blueprint_id": recipe["source_blueprint_id"],
        "base_probability": recipe["base_probability"],
        "probability": probability,
        "bpc_runs": max(1, recipe["output_runs"] + runs_mod),
        "me": max(0, min(10, BASE_ME + me_mod)),
        "te": max(0, min(20, BASE_TE + te_mod)),
        "time_seconds": recipe["time_seconds"],
        "materials": recipe["materials"],
        "decryptor": decryptor_name,
        "skills": science + encryption,
        "missing_skills": missing,
    }


def apply_invention(
    plan: dict,
    invention: dict,
    names: dict[int, str],
    quotes: dict[int, dict],
    adjusted: dict[int, float],
    product_materials: list[dict],
    decryptor_id: int | None,
    system_cost_index: float,
    cost_bonus: float,
    tax_rate: float,
) -> dict:
    if invention["probability"] <= 0:
        raise ValueError("Für diese Erfindung ist keine Erfolgswahrscheinlichkeit in der SDE hinterlegt.")
    blueprint_copies = math.ceil(plan["runs"] / invention["bpc_runs"])
    attempts = blueprint_copies / invention["probability"]
    lines = []
    missing = []
    inputs = [(item["type_id"], item["quantity"]) for item in invention["materials"]]
    if decryptor_id:
        inputs.append((decryptor_id, 1))
    for type_id, per_attempt in inputs:
        price = quotes.get(type_id, {}).get("lowest_sell")
        if price is None:
            price = adjusted.get(type_id)
        quantity = per_attempt * attempts
        if price is None:
            missing.append(names.get(type_id, str(type_id)))
        lines.append(
            {
                "type_id": type_id,
                "name": names.get(type_id, f"Item {type_id}"),
                "quantity": quantity,
                "unit_price": price,
                "total_cost": quantity * price if price is not None else None,
            }
        )
    eiv_prices = [adjusted.get(item["type_id"]) for item in product_materials]
    job_cost = None
    if product_materials and all(price is not None for price in eiv_prices):
        eiv = sum(
            price * item["quantity"] for price, item in zip(eiv_prices, product_materials)
        )
        job_cost = (
            eiv
            * JOB_COST_FACTOR
            * (system_cost_index / 100 * (1 - cost_bonus / 100) + tax_rate / 100 + 0.04)
            * attempts
        )
    total = (
        sum(line["total_cost"] for line in lines) + job_cost
        if not missing and job_cost is not None
        else None
    )
    invention.update(
        {
            "blueprint_copies": blueprint_copies,
            "attempts": attempts,
            "lines": lines,
            "job_cost": job_cost,
            "total_cost": total,
            "missing_prices": missing,
        }
    )
    plan["invention"] = invention
    if total is not None and plan.get("total_cost") is not None:
        plan["total_cost"] += total
        if plan.get("product_value") is not None:
            plan["margin"] = plan["product_value"] - plan["total_cost"]
    else:
        plan["total_cost"] = None
        plan["margin"] = None
    return plan
