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
                "location_id": location_id,
                "location_name": location_name,
                "location_missing": location_id not in locations,
                "location_flag": asset.get("location_flag", ""),
            }
        )
    return result
