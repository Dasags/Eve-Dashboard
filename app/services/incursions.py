import asyncio
import time

import httpx

from app.services.eve import ESIClient

TTL_SECONDS = 120
_cached_at = 0.0
_cached: list[dict] = []
_lock = asyncio.Lock()
_STATES = {
    "mobilizing": "Mobilisierung",
    "established": "Etabliert",
    "withdrawing": "Rückzug",
}


async def get_incursions() -> list[dict]:
    global _cached_at, _cached
    if time.monotonic() - _cached_at < TTL_SECONDS:
        return _cached
    async with _lock:
        if time.monotonic() - _cached_at < TTL_SECONDS:
            return _cached
        async with ESIClient() as esi:
            incursions = await esi.get("/incursions/")
            details = await asyncio.gather(
                *[
                    esi.get(f"/universe/constellations/{item['constellation_id']}/")
                    for item in incursions
                ]
            )
            region_ids = list({item["region_id"] for item in details})
            regions = await asyncio.gather(
                *[esi.get(f"/universe/regions/{region_id}/") for region_id in region_ids]
            )
            region_names = {
                region_id: region["name"]
                for region_id, region in zip(region_ids, regions, strict=True)
            }
            systems_needed = {
                system_id
                for item in incursions
                for system_id in (
                    [item["staging_solar_system_id"]]
                    + item.get("infested_solar_systems", [])
                )
            }
            systems = await asyncio.gather(
                *[esi.get(f"/universe/systems/{system_id}/") for system_id in systems_needed]
            )
            system_data = {
                system_id: system
                for system_id, system in zip(systems_needed, systems, strict=True)
            }
            names_needed = {
                item["faction_id"] for item in incursions if item.get("faction_id")
            }
            names_needed.update(item["constellation_id"] for item in incursions)
            if names_needed:
                names_response = await esi.post("/universe/names/", json=list(names_needed))
                names = {item["id"]: item["name"] for item in names_response}
            else:
                names = {}

        result = []
        for item, constellation in zip(incursions, details, strict=True):
            infested = [
                {
                    "id": system_id,
                    "name": system_data[system_id]["name"],
                    "security": system_data[system_id].get("security_status"),
                }
                for system_id in item.get("infested_solar_systems", [])
                if system_id in system_data
            ]
            result.append(
                {
                    "constellation": names.get(
                        item["constellation_id"], constellation["name"]
                    ),
                    "region": region_names[constellation["region_id"]],
                    "staging": system_data[item["staging_solar_system_id"]]["name"],
                    "staging_security": system_data[item["staging_solar_system_id"]].get(
                        "security_status"
                    ),
                    "faction": names.get(item.get("faction_id"), "Unbekannt"),
                    "state": _STATES.get(item["state"], item["state"]),
                    "has_boss": item.get("has_boss", False),
                    "influence": round(float(item.get("influence", 0)) * 100, 1),
                    "infested": infested,
                }
            )
        _cached = result
        _cached_at = time.monotonic()
        return _cached
