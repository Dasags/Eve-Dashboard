import asyncio

import httpx

from app.services.sde import as_int, field


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


def blueprint_records(assets: list[dict], sde: dict[str, list[dict[str, str]]]) -> list[dict]:
    types = {
        as_int(row, "typeID"): row
        for row in sde["invTypes"]
        if as_int(row, "typeID") > 0
    }
    groups = {
        as_int(row, "groupID"): row
        for row in sde["invGroups"]
        if as_int(row, "groupID") > 0
    }
    records = []
    for asset in assets:
        type_data = types.get(asset["type_id"])
        if not type_data:
            continue
        group_id = as_int(type_data, "groupID")
        group = groups.get(group_id)
        if not group or as_int(group, "categoryID") != 9:
            continue
        runs = asset.get("runs")
        copy = runs != -1
        records.append(
            {
                "item_id": int(asset["item_id"]),
                "type_id": int(asset["type_id"]),
                "type_name": field(type_data, "typeName", default=f"Type {asset['type_id']}"),
                "group_name": field(group, "groupName", default="Blueprints"),
                "is_copy": copy,
                "runs": runs if copy else None,
                "material_efficiency": asset.get("material_efficiency"),
                "time_efficiency": asset.get("time_efficiency"),
            }
        )
    return records


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
