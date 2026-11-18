from __future__ import annotations

from app.services.sde import as_int, field

SHIP_CATEGORY_ID = 6
MODULE_CATEGORY_ID = 7
CHARGE_CATEGORY_ID = 8
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
    "target_range": 76,
    "max_targets": 192,
    "scan_resolution": 564,
    "turret_hardpoints": 102,
    "launcher_hardpoints": 101,
    "shield_em": 271,
    "shield_thermal": 274,
    "shield_kinetic": 273,
    "shield_explosive": 272,
    "armor_em": 267,
    "armor_thermal": 270,
    "armor_kinetic": 269,
    "armor_explosive": 268,
    "hull_em": 113,
    "hull_thermal": 110,
    "hull_kinetic": 109,
    "hull_explosive": 111,
}

SLOT_EFFECT_IDS = {11: "low", 12: "high", 13: "medium", 2663: "rig", 3772: "subsystem"}
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
        key: attributes.get(attribute_id)
        for key, attribute_id in ATTRIBUTE_IDS.items()
    }
    slot = None
    if category == "module":
        for effect in data.get("dogma_effects", []):
            slot = SLOT_EFFECT_IDS.get(effect.get("effect_id"))
            if slot:
                break
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
