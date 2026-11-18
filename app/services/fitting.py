from __future__ import annotations

from app.services.sde import as_int, field

SHIP_CATEGORY_ID = 6
MODULE_CATEGORY_ID = 7
SUBSYSTEM_CATEGORY_ID = 32

ATTRIBUTE_IDS = {
    "powergrid": 11,
    "cpu": 48,
    "high_slots": 14,
    "medium_slots": 13,
    "low_slots": 12,
    "rig_slots": 1137,
    "subsystem_slots": 1367,
    "calibration": 1132,
    "drone_capacity": 283,
    "drone_bandwidth": 1272,
    "shield_hp": 263,
    "armor_hp": 265,
    "hull_hp": 9,
    "capacitor": 482,
    "capacitor_recharge": 55,
    "max_velocity": 37,
    "mass": 4,
    "cargo_capacity": 38,
    "signature_radius": 552,
}

SLOT_EFFECTS = {
    "hipower": "high",
    "medpower": "medium",
    "lopower": "low",
    "rigslot": "rig",
    "subsystem": "subsystem",
}


def catalog_item(type_id: int, row: dict, groups: dict[int, str]) -> dict:
    return {
        "id": type_id,
        "name": field(row, "typeName", "type_name", default=f"Type {type_id}"),
        "group": groups.get(as_int(row, "groupID", "group_id"), "Other"),
    }


def fitting_details(type_id: int, data: dict, category: str) -> dict:
    attributes = {
        int(item["attribute_id"]): float(item["value"])
        for item in data.get("dogma_attributes", [])
        if item.get("attribute_id") is not None
    }
    values = {
        key: attributes.get(attribute_id, 0)
        for key, attribute_id in ATTRIBUTE_IDS.items()
    }
    slot = None
    if category == "module":
        for effect in data.get("dogma_effects", []):
            name = str(effect.get("name", "")).casefold().replace("_", "")
            slot = next((value for key, value in SLOT_EFFECTS.items() if key in name), None)
            if slot:
                break
    return {
        "id": type_id,
        "name": data.get("name", f"Type {type_id}"),
        "category": category,
        "slot": slot,
        "attributes": values,
        "requirements": {
            "cpu": attributes.get(50, 0),
            "powergrid": attributes.get(30, 0),
            "calibration": attributes.get(1153, 0),
        },
    }
