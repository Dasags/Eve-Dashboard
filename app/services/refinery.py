import re
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR

from app.services.sde import as_int, field

MAX_LINES = 100
MAX_QUANTITY = 1_000_000_000_000
QUANTITY = re.compile(r"(?:\d+|\d{1,3}(?:[., '\u00a0\u202f]\d{3})+)")


def parse_inventory(text: str) -> tuple[dict[str, int], list[str]]:
    if len(text) > 30000:
        raise ValueError("Die Eingabe ist zu lang (maximal 30.000 Zeichen).")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_LINES:
        raise ValueError(f"Bitte höchstens {MAX_LINES} Zeilen auf einmal einfügen.")
    entries: dict[str, int] = {}
    errors = []
    for number, line in enumerate(lines, 1):
        columns = [part.strip() for part in line.split("\t")]
        if number == 1 and len(columns) >= 2 and columns[0].casefold() in {"name", "type", "item", "typ", "gegenstand"} and columns[1].casefold() in {"quantity", "menge", "anzahl"}:
            continue
        if len(columns) < 2:
            match = re.fullmatch(r"(.+?)\s+[xX]?\s*([\d.,']+)", line)
            columns = list(match.groups()) if match else columns
        if len(columns) < 2 or not columns[0] or not QUANTITY.fullmatch(columns[1]):
            errors.append(f"Zeile {number}: Name und ganzzahlige Menge nicht erkannt: {line[:160]}")
            continue
        quantity = int(re.sub(r"[., '\u00a0\u202f]", "", columns[1]))
        name = columns[0].casefold()
        if quantity <= 0 or entries.get(name, 0) + quantity > MAX_QUANTITY:
            errors.append(f"Zeile {number}: Menge muss zwischen 1 und {MAX_QUANTITY} liegen.")
            continue
        entries[name] = entries.get(name, 0) + quantity
    return entries, errors


def calculate(entries: dict[str, int], types: list[dict], materials: list[dict], yield_percent: Decimal) -> dict:
    type_rows = {as_int(row, "typeID"): row for row in types}
    by_name = {field(row, "typeName").casefold(): type_id for type_id, row in type_rows.items()}
    recipes: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in materials:
        source, material, quantity = (as_int(row, key) for key in ("typeID", "materialTypeID", "quantity"))
        if source > 0 and material > 0 and quantity > 0:
            recipes[source].append((material, quantity))
    inputs, errors = [], []
    output: dict[int, int] = defaultdict(int)
    for name, quantity in entries.items():
        type_id = by_name.get(name)
        row = type_rows.get(type_id)
        if not row or type_id not in recipes:
            errors.append(f"Nicht gefunden oder nicht wiederaufbereitbar: {name}")
            continue
        portion = as_int(row, "portionSize")
        if portion <= 0:
            errors.append(f"Keine gültige Losgröße für {field(row, 'typeName')}.")
            continue
        batches, remainder = divmod(quantity, portion)
        inputs.append({"type_id": type_id, "name": field(row, "typeName"), "quantity": quantity,
                       "portion": portion, "processed": batches * portion, "remainder": remainder})
        for material_id, base in recipes[type_id]:
            amount = int((Decimal(base * batches) * yield_percent / 100).to_integral_value(rounding=ROUND_FLOOR))
            if amount:
                output[material_id] += amount
    outputs = [
        {"type_id": type_id, "name": field(type_rows.get(type_id, {}), "typeName", default=f"Typ {type_id}"), "quantity": quantity}
        for type_id, quantity in output.items()
    ]
    return {"inputs": inputs, "outputs": sorted(outputs, key=lambda row: row["name"]), "errors": errors}


def apply_prices(result: dict, prices: dict[int, dict]) -> None:
    for row in result["inputs"] + result["outputs"]:
        quote = prices.get(row["type_id"], {})
        quantity = row.get("processed", row["quantity"])
        for side in ("highest_buy", "lowest_sell"):
            price = quote.get(side)
            row[side] = price
            row[f"{side}_value"] = price * quantity if price is not None else None
        row["price_error"] = quote.get("error")
    for group in ("inputs", "outputs"):
        rows = [row for row in result[group] if row.get("processed", row["quantity"]) > 0]
        for side in ("highest_buy", "lowest_sell"):
            values = [row[f"{side}_value"] for row in rows]
            can_be_zero = group == "outputs" and any(row["processed"] for row in result["inputs"])
            result[f"{group}_{side}"] = sum(values) if (rows or can_be_zero) and all(value is not None for value in values) and not result["errors"] else None
