import asyncio
import csv
import time
from pathlib import Path

import httpx

from app.services.storage import DATA_DIR

TABLES = (
    "invTypes",
    "invGroups",
    "industryActivity",
    "industryActivityMaterials",
    "industryActivityProducts",
    "industryActivityProbabilities",
    "industryActivitySkills",
    "planetSchematics",
    "planetSchematicsTypeMap",
)
BASE_URL = "https://www.fuzzwork.co.uk/dump/latest/csv"
CACHE_DIR = DATA_DIR / "sde"
TTL_SECONDS = 7 * 24 * 60 * 60
_cache: dict[str, list[dict[str, str]]] = {}
_cached_at = 0.0
_lock = asyncio.Lock()


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError(f"SDE-Datei enthält keine Spalten: {path.name}")
        return list(reader)


async def load_sde() -> dict[str, list[dict[str, str]]]:
    global _cache, _cached_at
    if _cache and time.monotonic() - _cached_at < TTL_SECONDS:
        return _cache
    async with _lock:
        if _cache and time.monotonic() - _cached_at < TTL_SECONDS:
            return _cache
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        paths = {name: CACHE_DIR / f"{name}.csv" for name in TABLES}
        stale = [
            name
            for name, path in paths.items()
            if not path.exists() or time.time() - path.stat().st_mtime >= TTL_SECONDS
        ]
        if stale:
            try:
                async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
                    responses = await asyncio.gather(
                        *(client.get(f"{BASE_URL}/{name}.csv") for name in stale)
                    )
                downloads = {}
                for name, response in zip(stale, responses, strict=True):
                    response.raise_for_status()
                    if not response.text.strip():
                        raise ValueError(f"Leere SDE-Datei erhalten: {name}.csv")
                    downloads[name] = response.text
                for name, content in downloads.items():
                    temporary = paths[name].with_suffix(".csv.tmp")
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(paths[name])
            except (httpx.HTTPError, OSError, ValueError):
                if not all(path.exists() for path in paths.values()):
                    raise
        _cache = {name: _read_csv(path) for name, path in paths.items()}
        _cached_at = time.monotonic()
        return _cache


def field(row: dict[str, str], *names: str, default: str = "") -> str:
    lowered = {key.casefold(): value for key, value in row.items()}
    for name in names:
        if name.casefold() in lowered and lowered[name.casefold()] not in (None, ""):
            return lowered[name.casefold()]
    return default


def as_int(row: dict[str, str], *names: str, default: int = 0) -> int:
    value = field(row, *names)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(row: dict[str, str], *names: str, default: float = 0) -> float:
    value = field(row, *names)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def type_catalog() -> tuple[dict[int, dict], dict[int, str]]:
    data = await load_sde()
    types = {
        as_int(row, "typeID"): row
        for row in data["invTypes"]
        if as_int(row, "typeID") > 0
    }
    groups = {
        as_int(row, "groupID"): field(row, "groupName")
        for row in data["invGroups"]
    }
    return types, groups


async def manufacturing_recipes() -> dict[int, dict]:
    data = await load_sde()
    materials: dict[int, list[dict]] = {}
    for row in data["industryActivityMaterials"]:
        activity_id = as_int(row, "activityID")
        if activity_id not in {1, 9}:
            continue
        materials.setdefault((as_int(row, "typeID"), activity_id), []).append(
            {
                "type_id": as_int(row, "materialTypeID"),
                "quantity": as_int(row, "quantity"),
            }
        )
    times = {
        (as_int(row, "typeID"), as_int(row, "activityID")): as_int(row, "time")
        for row in data["industryActivity"]
        if as_int(row, "activityID") in {1, 9}
    }
    recipes: dict[int, dict] = {}
    for row in data["industryActivityProducts"]:
        activity_id = as_int(row, "activityID")
        if activity_id not in {1, 9}:
            continue
        blueprint_id = as_int(row, "typeID")
        product_id = as_int(row, "productTypeID")
        key = (blueprint_id, activity_id)
        if key not in materials or product_id <= 0:
            continue
        recipes.setdefault(product_id, []).append(
            {
                "blueprint_id": blueprint_id,
                "product_id": product_id,
                "activity_id": activity_id,
                "activity": "Reaktion" if activity_id == 9 else "Herstellung",
                "output_quantity": max(1, as_int(row, "quantity", default=1)),
                "time_seconds": times.get(key, 0),
                "materials": materials[key],
            }
        )
    return recipes


async def planetary_recipes() -> dict[int, dict]:
    data = await load_sde()
    schematics = {
        as_int(row, "schematicID"): row for row in data["planetSchematics"]
    }
    grouped: dict[int, list[dict]] = {}
    outputs: dict[int, tuple[int, int]] = {}
    for row in data["planetSchematicsTypeMap"]:
        schematic_id = as_int(row, "schematicID")
        type_id = as_int(row, "typeID")
        quantity = as_int(row, "quantity")
        is_input = field(row, "isInput").casefold() in {"1", "true"}
        if schematic_id not in schematics or type_id <= 0 or quantity <= 0:
            continue
        if is_input:
            grouped.setdefault(schematic_id, []).append(
                {"type_id": type_id, "quantity": quantity}
            )
        else:
            outputs[type_id] = (schematic_id, quantity)
    recipes = {}
    for product_id, (schematic_id, output_quantity) in outputs.items():
        schematic = schematics[schematic_id]
        recipes[product_id] = {
            "product_id": product_id,
            "schematic_id": schematic_id,
            "name": field(schematic, "schematicName"),
            "cycle_seconds": as_int(schematic, "cycleTime"),
            "output_quantity": output_quantity,
            "materials": grouped.get(schematic_id, []),
        }
    return recipes


async def invention_recipes() -> dict[int, list[dict]]:
    data = await load_sde()
    materials: dict[int, list[dict]] = {}
    for row in data["industryActivityMaterials"]:
        if as_int(row, "activityID") == 8:
            materials.setdefault(as_int(row, "typeID"), []).append(
                {
                    "type_id": as_int(row, "materialTypeID"),
                    "quantity": as_int(row, "quantity"),
                }
            )
    times = {
        as_int(row, "typeID"): as_int(row, "time")
        for row in data["industryActivity"]
        if as_int(row, "activityID") == 8
    }
    skills: dict[int, list[dict]] = {}
    for row in data["industryActivitySkills"]:
        if as_int(row, "activityID") == 8:
            skills.setdefault(as_int(row, "typeID"), []).append(
                {"skill_id": as_int(row, "skillID"), "level": as_int(row, "level")}
            )
    probabilities = {
        (as_int(row, "typeID"), as_int(row, "productTypeID")): as_float(row, "probability")
        for row in data["industryActivityProbabilities"]
        if as_int(row, "activityID") == 8
    }
    result: dict[int, list[dict]] = {}
    for row in data["industryActivityProducts"]:
        if as_int(row, "activityID") != 8:
            continue
        source = as_int(row, "typeID")
        product = as_int(row, "productTypeID")
        if source <= 0 or product <= 0:
            continue
        result.setdefault(product, []).append(
            {
                "source_blueprint_id": source,
                "base_probability": probabilities.get((source, product), 0.0),
                "output_runs": max(1, as_int(row, "quantity", default=1)),
                "time_seconds": times.get(source, 0),
                "materials": materials.get(source, []),
                "skills": skills.get(source, []),
            }
        )
    for options in result.values():
        options.sort(key=lambda item: item["source_blueprint_id"])
    return result
