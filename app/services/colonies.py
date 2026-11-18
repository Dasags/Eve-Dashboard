from __future__ import annotations

import asyncio
from datetime import datetime, timezone

SCOPE = "esi-planets.manage_planets.v1"
SOON_SECONDS = 24 * 3600
PLANET_TYPES = {
    "temperate": "Temperate",
    "ice": "Ice",
    "gas": "Gas",
    "oceanic": "Oceanic",
    "lava": "Lava",
    "barren": "Barren",
    "storm": "Storm",
    "plasma": "Plasma",
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def status_for(seconds_left: float | None, has_extractors: bool) -> str:
    if not has_extractors:
        return "idle"
    if seconds_left is None or seconds_left <= 0:
        return "expired"
    return "soon" if seconds_left < SOON_SECONDS else "running"


def summarize(summary: dict, detail: dict, now: datetime | None = None) -> dict:
    """Verdichtet ESI-Planetendaten zu Extraktoren, Fabriken und Status."""
    now = now or datetime.now(timezone.utc)
    extractors = []
    factories: dict[int, int] = {}
    factory_pins: list[dict] = []
    storage_pins: list[dict] = []
    for pin in detail.get("pins", []):
        extractor = pin.get("extractor_details")
        if extractor:
            expiry = parse_time(pin.get("expiry_time"))
            extractors.append(
                {
                    "product_type_id": extractor.get("product_type_id"),
                    "seconds_left": (expiry - now).total_seconds() if expiry else None,
                    "expiry_time": expiry,
                    "heads": len(extractor.get("heads", [])),
                }
            )
        if not extractor and not pin.get("factory_details") and pin.get("contents") is not None:
            storage_pins.append(
                {
                    "type_id": pin.get("type_id"),
                    "contents": {int(c["type_id"]): int(c["amount"]) for c in pin["contents"] if c.get("amount")},
                }
            )
        factory = pin.get("factory_details")
        if factory and factory.get("schematic_id") is not None:
            schematic = int(factory["schematic_id"])
            factories[schematic] = factories.get(schematic, 0) + 1
            factory_pins.append(
                {"schematic_id": schematic, "last_cycle_start": parse_time(pin.get("last_cycle_start"))}
            )
    extractors.sort(key=lambda item: item["seconds_left"] if item["seconds_left"] is not None else -1)
    soonest = extractors[0]["seconds_left"] if extractors else None
    planet_type = str(summary.get("planet_type", "")).lower()
    return {
        "planet_id": summary["planet_id"],
        "system_id": summary.get("solar_system_id"),
        "planet_type": PLANET_TYPES.get(planet_type, planet_type.title() or "Unbekannt"),
        "planet_type_key": planet_type,
        "command_center_level": summary.get("upgrade_level", 0),
        "pin_count": summary.get("num_pins", len(detail.get("pins", []))),
        "extractors": extractors,
        "factories": factories,
        "factory_pins": factory_pins,
        "storage_pins": storage_pins,
        "seconds_left": soonest,
        "status": status_for(soonest, bool(extractors)),
    }


async def load_colonies(esi, character_id: int, concurrency: int = 5) -> list[dict]:
    summaries = await esi.get(f"/characters/{character_id}/planets/")
    gate = asyncio.Semaphore(concurrency)

    async def one(summary: dict) -> dict:
        async with gate:
            detail = await esi.get(f"/characters/{character_id}/planets/{summary['planet_id']}/")
        return summarize(summary, detail)

    colonies = await asyncio.gather(*(one(item) for item in summaries))
    order = {"expired": 0, "soon": 1, "running": 2, "idle": 3}
    return sorted(
        colonies,
        key=lambda item: (order[item["status"]], item["seconds_left"] if item["seconds_left"] is not None else -1),
    )


def factory_rings(colony: dict, cycle_times: dict[int, int], names: dict[int, str], now: datetime | None = None) -> list[dict]:
    """Zyklusfortschritt je Fabrik; Ring zeigt den Anteil des aktuellen Zyklus."""
    now = now or datetime.now(timezone.utc)
    rings = []
    for pin in colony.get("factory_pins", []):
        cycle = cycle_times.get(pin["schematic_id"])
        start = pin["last_cycle_start"]
        name = names.get(pin["schematic_id"], f"Schema {pin['schematic_id']}")
        if not cycle or not start:
            rings.append({"name": name, "status": "idle", "seconds_left": None, "fraction": 0, "cycle": cycle})
            continue
        left = cycle - (now - start).total_seconds()
        # Läuft ein Zyklus weiter, startet die Fabrik laut ESI neu; nach Ablauf wartet sie auf Material.
        status = "expired" if left <= 0 else "running"
        rings.append(
            {
                "name": name,
                "status": status,
                "seconds_left": left,
                "fraction": 0 if left <= 0 else min(1, left / cycle),
                "cycle": cycle,
            }
        )
    rings.sort(key=lambda r: (r["seconds_left"] is None, r["seconds_left"] or 0))
    return rings


def storage_kind(type_name: str) -> str | None:
    name = type_name.lower()
    if "launchpad" in name:
        return "Launchpad"
    if "storage" in name:
        return "Lager"
    if "command center" in name:
        return "Command Center"
    return None


def storage_fill(colony: dict, types: dict[int, dict]) -> list[dict]:
    """Füllstand je Lager/Launchpad aus Inhalt (m³) und Kapazität der Struktur."""
    result = []
    for pin in colony.get("storage_pins", []):
        info = types.get(pin["type_id"]) or {}
        kind = storage_kind(info.get("name", ""))
        capacity = float(info.get("capacity") or 0)
        if not kind or capacity <= 0:
            continue
        used = sum(float((types.get(tid) or {}).get("volume") or 0) * amount for tid, amount in pin["contents"].items())
        fraction = min(1.0, used / capacity)
        status = "expired" if fraction >= 0.9 else "soon" if fraction >= 0.7 else "running"
        result.append({"kind": kind, "used": used, "capacity": capacity, "fraction": fraction, "percent": round(fraction * 100), "status": status})
    order = {"Launchpad": 0, "Lager": 1, "Command Center": 2}
    return sorted(result, key=lambda r: (order[r["kind"]], -r["fraction"]))
