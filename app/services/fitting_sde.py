from __future__ import annotations

import asyncio
import csv
import time

import httpx

from app.services.sde import BASE_URL, CACHE_DIR, TTL_SECONDS, as_int, field

TABLES = ("invMarketGroups", "dgmTypeAttributes", "dgmTypeEffects")

ATTR_CPU_NEED = 50
ATTR_POWERGRID_NEED = 30
ATTR_RIG_SIZE = 1547
ATTR_CPU = 48
ATTR_POWERGRID = 11
ATTR_HIGH, ATTR_MEDIUM, ATTR_LOW = 14, 13, 12
ATTR_RIG, ATTR_SUBSYSTEM = 1137, 1367
ATTR_TURRETS, ATTR_LAUNCHERS = 102, 101
CAN_FIT_GROUP = (1298, 1299, 1300, 1301, 1872, 1879, 1880, 1881)
CAN_FIT_TYPE = (1302, 1303, 1304, 1305, 1944, 2103, 2463, 2486, 2487, 2488)
NEEDED_ATTRIBUTES = {
    ATTR_CPU_NEED, ATTR_POWERGRID_NEED, ATTR_RIG_SIZE, ATTR_CPU, ATTR_POWERGRID,
    ATTR_HIGH, ATTR_MEDIUM, ATTR_LOW, ATTR_RIG, ATTR_SUBSYSTEM, ATTR_TURRETS, ATTR_LAUNCHERS,
    *CAN_FIT_GROUP, *CAN_FIT_TYPE,
}
SLOT_EFFECT_IDS = {11: "low", 12: "high", 13: "medium", 2663: "rig", 3772: "subsystem"}
EFFECT_TURRET, EFFECT_LAUNCHER = 42, 40
NEEDED_EFFECTS = {*SLOT_EFFECT_IDS, EFFECT_TURRET, EFFECT_LAUNCHER}
SLOT_ATTRIBUTE = {
    "high": ATTR_HIGH, "medium": ATTR_MEDIUM, "low": ATTR_LOW,
    "rig": ATTR_RIG, "subsystem": ATTR_SUBSYSTEM,
}
UNGROUPED = "Ohne Marktgruppe"

_cache: dict | None = None
_cached_at = 0.0
_lock = asyncio.Lock()


def _parse(texts: dict[str, str]) -> dict:
    groups = {}
    for row in csv.DictReader(texts["invMarketGroups"].splitlines()):
        group_id = as_int(row, "marketGroupID")
        if group_id:
            groups[group_id] = {
                "parent": as_int(row, "parentGroupID") or None,
                "name": field(row, "marketGroupName", default=f"Gruppe {group_id}"),
            }
    attributes: dict[int, dict[int, float]] = {}
    for row in csv.DictReader(texts["dgmTypeAttributes"].splitlines()):
        attribute_id = as_int(row, "attributeID")
        if attribute_id not in NEEDED_ATTRIBUTES:
            continue
        raw = field(row, "valueFloat") or field(row, "valueInt")
        try:
            value = float(raw)
        except ValueError:
            continue
        attributes.setdefault(as_int(row, "typeID"), {})[attribute_id] = value
    effects: dict[int, set[int]] = {}
    for row in csv.DictReader(texts["dgmTypeEffects"].splitlines()):
        effect_id = as_int(row, "effectID")
        if effect_id in NEEDED_EFFECTS:
            effects.setdefault(as_int(row, "typeID"), set()).add(effect_id)
    return {"market_groups": groups, "attributes": attributes, "effects": effects}


async def load_fitting_sde() -> dict:
    global _cache, _cached_at
    if _cache and time.monotonic() - _cached_at < TTL_SECONDS:
        return _cache
    async with _lock:
        if _cache and time.monotonic() - _cached_at < TTL_SECONDS:
            return _cache
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        paths = {name: CACHE_DIR / f"{name}.csv" for name in TABLES}
        stale = [
            name for name, path in paths.items()
            if not path.exists() or time.time() - path.stat().st_mtime >= TTL_SECONDS
        ]
        if stale:
            try:
                async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                    responses = await asyncio.gather(*(client.get(f"{BASE_URL}/{name}.csv") for name in stale))
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
        texts = {name: path.read_text(encoding="utf-8-sig") for name, path in paths.items()}
        _cache = await asyncio.to_thread(_parse, texts)
        _cached_at = time.monotonic()
        return _cache


def hull_profile(type_id: int, group_id: int, attributes: dict[int, float]) -> dict:
    return {
        "type_id": type_id,
        "group_id": group_id,
        "slots": {slot: attributes.get(attr, 0) for slot, attr in SLOT_ATTRIBUTE.items()},
        "turrets": attributes.get(ATTR_TURRETS, 0),
        "launchers": attributes.get(ATTR_LAUNCHERS, 0),
        "rig_size": attributes.get(ATTR_RIG_SIZE),
        "cpu": attributes.get(ATTR_CPU, 0),
        "powergrid": attributes.get(ATTR_POWERGRID, 0),
    }


def fits_hull(attributes: dict[int, float], effects: set[int], hull: dict) -> bool:
    slot = next((SLOT_EFFECT_IDS[effect] for effect in effects if effect in SLOT_EFFECT_IDS), None)
    if slot and hull["slots"].get(slot, 0) <= 0:
        return False
    if EFFECT_TURRET in effects and hull["turrets"] <= 0:
        return False
    if EFFECT_LAUNCHER in effects and hull["launchers"] <= 0:
        return False
    if slot == "rig" and hull["rig_size"] is not None:
        rig_size = attributes.get(ATTR_RIG_SIZE)
        if rig_size is not None and rig_size != hull["rig_size"]:
            return False
    allowed_groups = {int(attributes[attr]) for attr in CAN_FIT_GROUP if attr in attributes}
    allowed_types = {int(attributes[attr]) for attr in CAN_FIT_TYPE if attr in attributes}
    if (allowed_groups or allowed_types) and hull["group_id"] not in allowed_groups \
            and hull["type_id"] not in allowed_types:
        return False
    if attributes.get(ATTR_CPU_NEED, 0) > hull["cpu"] or attributes.get(ATTR_POWERGRID_NEED, 0) > hull["powergrid"]:
        return False
    return True


def build_market_tree(
    items: list[dict], market_groups: dict[int, dict]
) -> list[dict]:
    """Nest items (each with `market_group`) into their market-group hierarchy."""
    nodes: dict[int, dict] = {}

    def node(group_id: int | None) -> dict:
        key = group_id or 0
        if key not in nodes:
            info = market_groups.get(key, {})
            nodes[key] = {
                "id": key,
                "name": info.get("name", UNGROUPED),
                "parent": info.get("parent"),
                "children": [],
                "items": [],
            }
        return nodes[key]

    for item in items:
        node(item["market_group"])["items"].append(
            {key: value for key, value in item.items() if key != "market_group"}
        )
    # Create ancestors so each leaf is reachable from a root.
    for group_id in list(nodes):
        current = nodes[group_id]
        seen = {group_id}
        while current["parent"] and current["parent"] not in seen:
            seen.add(current["parent"])
            current = node(current["parent"])
    roots = []
    for current in nodes.values():
        parent = nodes.get(current["parent"]) if current["parent"] else None
        (parent["children"] if parent else roots).append(current)

    def finish(current: dict) -> int:
        current["children"].sort(key=lambda child: child["name"].casefold())
        current["items"].sort(key=lambda item: item["name"].casefold())
        current["count"] = len(current["items"]) + sum(finish(child) for child in current["children"])
        current.pop("parent", None)
        return current["count"]

    for root in roots:
        finish(root)
    roots.sort(key=lambda root: root["name"].casefold())
    while len(roots) == 1 and not roots[0]["items"] and roots[0]["children"]:
        roots = roots[0]["children"]
    return roots
