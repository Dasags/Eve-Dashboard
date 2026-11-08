import asyncio
import time

import httpx

from app.services import storage
from app.services.eve import ESIClient

JITA_REGION_ID = 10000002
_adjusted_prices: dict[int, float] = {}
_adjusted_at = 0.0
_adjusted_lock = asyncio.Lock()


async def adjusted_prices(type_ids: set[int]) -> dict[int, float]:
    global _adjusted_at, _adjusted_prices
    if not _adjusted_prices or time.monotonic() - _adjusted_at > 3600:
        async with _adjusted_lock:
            if not _adjusted_prices or time.monotonic() - _adjusted_at > 3600:
                async with ESIClient() as esi:
                    rows = await esi.get("/markets/prices/")
                _adjusted_prices = {
                    row["type_id"]: float(row["adjusted_price"])
                    for row in rows
                    if row.get("adjusted_price") is not None
                }
                _adjusted_at = time.monotonic()
    return {type_id: _adjusted_prices[type_id] for type_id in type_ids if type_id in _adjusted_prices}


async def _paginated_orders(
    esi: ESIClient,
    path: str,
    params: dict,
) -> list[dict]:
    first = await esi.request("GET", path, params={**params, "page": 1})
    orders = first.json()
    try:
        page_count = max(1, int(first.headers.get("X-Pages", "1")))
    except ValueError as exc:
        raise ValueError("Der Marktstandort lieferte eine ungültige ESI-Seitenanzahl.") from exc
    if page_count > 100:
        raise ValueError("Der Marktstandort enthält mehr als 100 ESI-Seiten.")
    pages = page_count
    if pages > 1:
        responses = await asyncio.gather(
            *[
                esi.get(path, params={**params, "page": page})
                for page in range(2, pages + 1)
            ]
        )
        for page in responses:
            orders.extend(page)
    return orders


async def _resolve_region(location: dict) -> int:
    if location.get("region_id"):
        return int(location["region_id"])
    character_id = location.get("character_id") if location["kind"] == "structure" else None
    async with ESIClient(character_id) as esi:
        if location["kind"] == "station":
            address = await esi.get(f"/universe/stations/{location['location_id']}/")
        else:
            address = await esi.get(f"/universe/structures/{location['location_id']}/")
        system = await esi.get(f"/universe/systems/{address['solar_system_id']}/")
        constellation = await esi.get(
            f"/universe/constellations/{system['constellation_id']}/"
        )
    region_id = int(constellation["region_id"])
    storage.set_location_region(location["id"], region_id)
    return region_id


async def quote(location: dict, type_id: int) -> dict:
    character_id = location.get("character_id") if location["kind"] == "structure" else None
    if location["kind"] == "structure":
        if not character_id:
            raise PermissionError("Für diese Structure ist kein Abfrage-Charakter festgelegt.")
        token = storage.get_character(character_id)
        if not token or "esi-markets.structure_markets.v1" not in token["scopes"]:
            raise PermissionError("Dem Structure-Charakter fehlt esi-markets.structure_markets.v1.")
    region_id = await _resolve_region(location)
    async with ESIClient(character_id) as esi:
        if location["kind"] == "structure":
            orders = await _paginated_orders(
                esi,
                f"/markets/structures/{location['location_id']}/",
                {},
            )
        else:
            orders = await _paginated_orders(
                esi,
                f"/markets/{region_id}/orders/",
                {"order_type": "all", "type_id": type_id},
            )
            orders = [
                order
                for order in orders
                if order["location_id"] == location["location_id"]
            ]
        sell_prices = [
            float(order["price"])
            for order in orders
            if order["type_id"] == type_id and not order["is_buy_order"]
        ]
        buy_prices = [
            float(order["price"])
            for order in orders
            if order["type_id"] == type_id and order["is_buy_order"]
        ]
        history = await esi.get(
            f"/markets/{region_id}/history/",
            params={"type_id": type_id},
        )
    history = sorted(history, key=lambda row: row["date"], reverse=True)
    change = None
    if len(history) > 1 and history[1].get("average"):
        change = (
            float(history[0]["average"]) - float(history[1]["average"])
        ) / float(history[1]["average"]) * 100
    return {
        "lowest_sell": min(sell_prices) if sell_prices else None,
        "highest_buy": max(buy_prices) if buy_prices else None,
        "change_24h": change,
        "region_id": region_id,
    }


async def sell_prices(location: dict, type_ids: list[int]) -> dict[int, dict]:
    """Fetch station orders per type, or a structure's complete order book once."""
    character_id = location.get("character_id") if location["kind"] == "structure" else None
    if location["kind"] == "structure":
        if not character_id:
            raise PermissionError("Für diese Structure ist kein Abfrage-Charakter festgelegt.")
        character = storage.get_character(character_id)
        if not character or "esi-markets.structure_markets.v1" not in character["scopes"]:
            raise PermissionError("Dem Structure-Charakter fehlt esi-markets.structure_markets.v1.")
    region_id = await _resolve_region(location) if location["kind"] == "station" else None
    async with ESIClient(character_id) as esi:
        if location["kind"] == "structure":
            orders = await _paginated_orders(esi, f"/markets/structures/{location['location_id']}/", {})
            prices: dict[int, list[float]] = {type_id: [] for type_id in type_ids}
            for order in orders:
                if order["type_id"] in prices and not order["is_buy_order"]:
                    prices[order["type_id"]].append(float(order["price"]))
            return {
                type_id: {"lowest_sell": min(values) if values else None, "error": None}
                for type_id, values in prices.items()
            }

        semaphore = asyncio.Semaphore(3)

        async def station_price(type_id: int) -> tuple[int, dict]:
            try:
                async with semaphore:
                    orders = await _paginated_orders(
                        esi, f"/markets/{region_id}/orders/", {"order_type": "sell", "type_id": type_id}
                    )
                prices = [
                    float(order["price"]) for order in orders
                    if order["location_id"] == location["location_id"]
                    and order["type_id"] == type_id and not order["is_buy_order"]
                ]
                return type_id, {"lowest_sell": min(prices) if prices else None, "error": None}
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                return type_id, {"lowest_sell": None, "error": str(exc) or "Marktdaten nicht erreichbar"}

        return dict(await asyncio.gather(*(station_price(type_id) for type_id in type_ids)))


async def resolve_type_ids(names: list[str]) -> dict[str, int]:
    cleaned = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not cleaned:
        return {}
    async with ESIClient() as esi:
        result = await esi.post("/universe/ids/", json=cleaned)
    types = result.get("inventory_types", [])
    by_name = {item["name"].casefold(): item["id"] for item in types}
    return {name: by_name[name.casefold()] for name in cleaned if name.casefold() in by_name}
