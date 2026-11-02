import asyncio
import csv
import json
import time
from pathlib import Path

import httpx

from app.services.storage import DATA_DIR

_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"
_CACHE = DATA_DIR / "cache" / "eve-map.json"
_TTL_SECONDS = 24 * 60 * 60
_cached_at = 0.0
_cached: dict | None = None
_VERSION = 2
# Wurmloch-, Abyssal- und Jove-Regionen liegen weit außerhalb von New Eden
_EXCLUDED_REGIONS = {10000004, 10000017, 10000019}


def _is_known_space(region_id: int) -> bool:
    return 10000000 <= region_id < 11000000 and region_id not in _EXCLUDED_REGIONS
_lock = asyncio.Lock()


async def _csv(client: httpx.AsyncClient, filename: str) -> list[dict[str, str]]:
    response = await client.get(f"{_BASE}/{filename}")
    response.raise_for_status()
    reader = csv.DictReader(response.text.lstrip("\ufeff").splitlines())
    if not reader.fieldnames:
        raise ValueError(f"Fuzzwork lieferte eine ungültige CSV-Datei: {filename}")
    return list(reader)


async def _download() -> dict:
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        systems, connections, regions = await asyncio.gather(
            _csv(client, "mapSolarSystems.csv"),
            _csv(client, "mapSolarSystemJumps.csv"),
            _csv(client, "mapRegions.csv"),
        )
    region_names = {int(row["regionID"]): row["regionName"] for row in regions}
    system_rows = []
    for row in systems:
        try:
            region_id = int(row["regionID"])
            if not _is_known_space(region_id):
                continue
            system_rows.append(
                {
                    "id": int(row["solarSystemID"]),
                    "name": row["solarSystemName"],
                    "region_id": region_id,
                    "region": region_names.get(region_id, str(region_id)),
                    "x": float(row["x"]),
                    "y": float(row["z"]),
                    "security": (
                        float(row.get("security") or row.get("securityStatus"))
                        if row.get("security") or row.get("securityStatus")
                        else None
                    ),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    valid_ids = {row["id"] for row in system_rows}
    edges = {
        tuple(sorted((int(row["fromSolarSystemID"]), int(row["toSolarSystemID"]))))
        for row in connections
        if int(row["fromSolarSystemID"]) in valid_ids
        and int(row["toSolarSystemID"]) in valid_ids
    }
    if not system_rows:
        raise ValueError("Die Fuzzwork-Kartendaten enthalten keine gültigen Systeme.")
    xs = [row["x"] for row in system_rows]
    ys = [row["y"] for row in system_rows]
    grouped: dict[int, list[dict]] = {}
    for row in system_rows:
        grouped.setdefault(row["region_id"], []).append(row)
    return {
        "version": _VERSION,
        "systems": system_rows,
        "connections": [list(edge) for edge in sorted(edges)],
        "regions": sorted({row["region"] for row in system_rows}),
        "region_centers": [
            {
                "region_id": region_id,
                "name": region_names.get(region_id, str(region_id)),
                "x": sum(system["x"] for system in items) / len(items),
                "y": sum(system["y"] for system in items) / len(items),
            }
            for region_id, items in grouped.items()
        ],
        "bounds": [min(xs), max(xs), min(ys), max(ys)],
    }


async def get_map_data() -> dict:
    global _cached_at, _cached
    if _cached and time.monotonic() - _cached_at < _TTL_SECONDS:
        return _cached
    async with _lock:
        if _cached and time.monotonic() - _cached_at < _TTL_SECONDS:
            return _cached
        if _CACHE.exists() and time.time() - _CACHE.stat().st_mtime < _TTL_SECONDS:
            cached = json.loads(_CACHE.read_text(encoding="utf-8"))
            if cached.get("version") == _VERSION and all(
                isinstance(system.get("security"), (int, float))
                for system in cached.get("systems", [])
            ) and all(
                isinstance(region.get("region_id"), int)
                for region in cached.get("region_centers", [])
            ):
                _cached = cached
                _cached_at = time.monotonic()
                return _cached
        try:
            result = await _download()
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            if _CACHE.exists():
                _cached = json.loads(_CACHE.read_text(encoding="utf-8"))
                region_ids = {
                    system["region"]: system["region_id"]
                    for system in _cached.get("systems", [])
                }
                for system in _cached.get("systems", []):
                    system.setdefault("security", None)
                for region in _cached.get("region_centers", []):
                    region.setdefault("region_id", region_ids.get(region["name"]))
                _cached_at = time.monotonic()
                return _cached
            raise
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
        _cached = result
        _cached_at = time.monotonic()
        return result
