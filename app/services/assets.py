import asyncio

import httpx


async def blueprint_types(esi, type_ids: set[int]) -> dict[int, bool | None]:
    """Resolve the ESI category of visible non-copy assets; None means unavailable."""
    semaphore = asyncio.Semaphore(10)

    async def get_type(type_id: int) -> tuple[int, int | None]:
        try:
            async with semaphore:
                data = await esi.get(f"/universe/types/{type_id}/")
            return type_id, int(data["group_id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return type_id, None

    groups_by_type = dict(await asyncio.gather(*(get_type(type_id) for type_id in type_ids)))

    async def get_group(group_id: int) -> tuple[int, bool | None]:
        try:
            async with semaphore:
                data = await esi.get(f"/universe/groups/{group_id}/")
            return group_id, int(data["category_id"]) == 9
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return group_id, None

    group_ids = {group_id for group_id in groups_by_type.values() if group_id is not None}
    categories = dict(await asyncio.gather(*(get_group(group_id) for group_id in group_ids)))
    return {
        type_id: categories.get(group_id)
        for type_id, group_id in groups_by_type.items()
    }


def assemble_assets(
    assets: list[dict], names: dict[int, str], locations: dict[int, str]
) -> list[dict]:
    """Resolve nested containers and ships to their outermost ESI location."""
    by_id = {asset["item_id"]: asset for asset in assets}
    result = []
    for asset in assets:
        current = asset
        seen = set()
        location_id = current["location_id"]
        while current.get("location_type") == "item" and location_id in by_id:
            if location_id in seen:
                break
            seen.add(location_id)
            current = by_id[location_id]
            location_id = current["location_id"]
        location_name = locations.get(location_id)
        if not location_name:
            location_name = f"Unbekannter Standort (ID {location_id})"
        result.append(
            {
                "item_id": asset["item_id"],
                "type_id": asset["type_id"],
                "name": names.get(asset["type_id"], f"Unbekannter Gegenstand (ID {asset['type_id']})"),
                "quantity": asset["quantity"],
                "is_blueprint_copy": asset.get("is_blueprint_copy", False),
                "location_id": location_id,
                "location_name": location_name,
                "location_missing": location_id not in locations,
                "location_flag": asset.get("location_flag", ""),
            }
        )
    return result
