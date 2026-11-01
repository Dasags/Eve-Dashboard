import asyncio
import os
import time

import httpx

ESI = "https://esi.evetech.net/latest"
ZKILL = "https://zkillboard.com/api"
TTL = 60

_cache: dict = {"ts": 0.0, "data": []}
_lock = asyncio.Lock()


def _headers() -> dict:
    contact = os.getenv("CONTACT", "unknown")
    return {"User-Agent": f"{os.getenv('APP_NAME', 'EVE-World')} {contact}"}


async def _fetch(region_id: str | None, limit: int) -> list[dict]:
    path = f"/kills/regionID/{region_id}/" if region_id else "/kills/"
    async with httpx.AsyncClient(headers=_headers(), timeout=15) as c:
        r = await c.get(ZKILL + path)
        r.raise_for_status()
        refs = r.json()[:limit]

        async def km(ref: dict):
            try:
                resp = await c.get(f"{ESI}/killmails/{ref['killmail_id']}/{ref['zkb']['hash']}/")
                resp.raise_for_status()
                return {**resp.json(), "value": ref["zkb"].get("totalValue", 0)}
            except (httpx.HTTPError, KeyError):
                return None

        kms = [k for k in await asyncio.gather(*(km(x) for x in refs)) if k]

        ids = set()
        for k in kms:
            v = k["victim"]
            ids.update(i for i in (k["solar_system_id"], v.get("ship_type_id"), v.get("character_id")) if i)
        names: dict[int, str] = {}
        if ids:
            try:
                nr = await c.post(f"{ESI}/universe/names/", json=list(ids))
                nr.raise_for_status()
                names = {n["id"]: n["name"] for n in nr.json()}
            except httpx.HTTPError:
                pass

    return [
        {
            "id": k["killmail_id"],
            "time": k["killmail_time"],
            "system": names.get(k["solar_system_id"], str(k["solar_system_id"])),
            "ship": names.get(k["victim"].get("ship_type_id"), "?"),
            "victim": names.get(k["victim"].get("character_id"), "NPC/Struktur"),
            "value": k["value"],
        }
        for k in kms
    ]


async def get_kills(limit: int = 15) -> list[dict]:
    async with _lock:
        if time.time() - _cache["ts"] < TTL:
            return _cache["data"]
        try:
            _cache["data"] = await _fetch(os.getenv("KILLFEED_REGION") or None, limit)
        except httpx.HTTPError:
            pass  # alte Daten behalten
        _cache["ts"] = time.time()
        return _cache["data"]
