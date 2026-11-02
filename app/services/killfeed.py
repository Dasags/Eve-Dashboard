import asyncio
import os
import time

import httpx

ESI = "https://esi.evetech.net/latest"
ZKILL = "https://zkillboard.com/api"
TTL = 60
_cache: dict[tuple[tuple[str, ...], int], tuple[float, list[dict]]] = {}
_lock = asyncio.Lock()


def _headers() -> dict:
    contact = os.getenv("CONTACT", "configure CONTACT")
    return {"User-Agent": f"{os.getenv('APP_NAME', 'EVE-World')} ({contact})"}


async def _region_ids(client: httpx.AsyncClient, regions: list[str]) -> list[int]:
    region_ids: list[int] = []
    names: list[str] = []
    for region in regions:
        if region.isdecimal():
            region_ids.append(int(region))
        else:
            names.append(region)
    if names:
        response = await client.post(f"{ESI}/universe/ids/", json=names)
        response.raise_for_status()
        resolved = response.json().get("regions", [])
        found = {item["name"].casefold(): item["id"] for item in resolved}
        missing = [name for name in names if name.casefold() not in found]
        if missing:
            raise ValueError(f"Unbekannte EVE-Region(en): {', '.join(missing)}")
        region_ids.extend(found[name.casefold()] for name in names)
    return list(dict.fromkeys(region_ids))


async def _fetch(regions: list[str], limit: int) -> list[dict]:
    async with httpx.AsyncClient(headers=_headers(), timeout=20) as client:
        region_ids = await _region_ids(client, regions) if regions else []
        paths = (
            [f"/kills/regionID/{region_id}/" for region_id in region_ids]
            if region_ids
            else ["/kills/"]
        )
        responses = await asyncio.gather(
            *(client.get(f"{ZKILL}{path}") for path in paths)
        )
        references: dict[int, dict] = {}
        for response in responses:
            response.raise_for_status()
            for reference in response.json():
                references[reference["killmail_id"]] = reference

        semaphore = asyncio.Semaphore(10)

        async def killmail(reference: dict) -> dict | None:
            async with semaphore:
                response = await client.get(
                    f"{ESI}/killmails/{reference['killmail_id']}/{reference['zkb']['hash']}/"
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                data = response.json()
                data["value"] = reference["zkb"].get("totalValue", 0)
                return data

        mails = [
            item
            for item in await asyncio.gather(
                *(killmail(reference) for reference in references.values())
            )
            if item
        ]
        mails.sort(key=lambda item: item["killmail_time"], reverse=True)
        mails = mails[:limit]
        ids = {
            object_id
            for mail in mails
            for object_id in (
                mail.get("solar_system_id"),
                mail.get("victim", {}).get("ship_type_id"),
                mail.get("victim", {}).get("character_id"),
            )
            if object_id
        }
        names: dict[int, str] = {}
        if ids:
            response = await client.post(f"{ESI}/universe/names/", json=list(ids))
            response.raise_for_status()
            names = {item["id"]: item["name"] for item in response.json()}

    return [
        {
            "id": mail["killmail_id"],
            "time": mail["killmail_time"],
            "system_id": mail["solar_system_id"],
            "system": names.get(mail["solar_system_id"], str(mail["solar_system_id"])),
            "ship": names.get(mail["victim"].get("ship_type_id"), "Unbekannt"),
            "victim": names.get(mail["victim"].get("character_id"), "NPC/Struktur"),
            "value": mail["value"],
        }
        for mail in mails
    ]


async def get_kills(regions: list[str] | None = None, limit: int = 15) -> list[dict]:
    requested_regions = tuple(
        sorted({region.strip() for region in (regions or []) if region.strip()}, key=str.casefold)
    )
    if len(requested_regions) > 20:
        raise ValueError("Es können höchstens 20 Regionen gleichzeitig abgefragt werden.")
    limit = min(50, max(5, int(limit)))
    key = (requested_regions, limit)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < TTL:
        return cached[1][:limit]
    async with _lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < TTL:
            return cached[1][:limit]
        result = await _fetch(list(requested_regions), limit)
        _cache[key] = (time.time(), result)
        return result[:limit]
