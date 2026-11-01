import asyncio
import hmac
import math
import os
import re
import secrets
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.services import storage
from app.services.eve import ESIClient, authorization_url, exchange_code
from app.services.incursions import get_incursions
from app.services.killfeed import get_kills
from app.services.map_data import get_map_data
from app.services.market import quote as get_market_quote
from app.services.market import resolve_type_ids
from app.services.market import adjusted_prices as get_adjusted_prices
from app.services.invention import DECRYPTORS, apply_invention, plan_invention
from app.services.production import apply_market_and_stock, create_plan, skill_time_factor
from app.services.planetary import add_location_prices, create_plan as create_planetary_plan
from app.services.sde import field, invention_recipes, manufacturing_recipes, planetary_recipes, type_catalog

APP_NAME = os.getenv("APP_NAME", "EVE-World")
MODULE_DETAILS = {
    "dashboard": ("Char-Dashboard", "Charakter, Corporation, Allianz und MOTD."),
    "skills": ("Skills", "Skill-Warteschlange und Skill-Übersicht."),
    "wallet": ("Wallet", "Kontostand, Journal und Wallet-Verteilung."),
    "industry": ("Industry", "Produktionskosten, Materialbedarf und Lagerbestand."),
    "incursions": ("Incursion", "Aktuelle Sansha-Incursions aus öffentlichen ESI-Daten."),
    "marketwatcher": ("Marketwatcher", "Item-Listen und regionale Marktpreise."),
    "planetary-industry": ("Planetary Industry", "PI-Produktionsketten und Standortpreise."),
}
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")

app = FastAPI(title=APP_NAME)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(64),
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true",
)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def format_isk(value: float | int) -> str:
    return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_duration(seconds: float | int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


templates.env.filters["isk"] = format_isk
templates.env.filters["duration"] = format_duration


async def resolve_names(esi: ESIClient, ids: set[int]) -> dict[int, str]:
    result: dict[int, str] = {}
    values = sorted(ids)
    for offset in range(0, len(values), 500):
        response = await esi.post("/universe/names/", json=values[offset : offset + 500])
        result.update({item["id"]: item["name"] for item in response})
    return result


async def paginated_list(
    esi: ESIClient,
    path: str,
    params: dict | None = None,
    max_pages: int = 100,
) -> list[dict]:
    request_params = dict(params or {})
    first = await esi.request("GET", path, params={**request_params, "page": 1})
    try:
        pages = int(first.headers.get("X-Pages", "1"))
    except ValueError as exc:
        raise RuntimeError("ESI lieferte eine ungültige Seitenanzahl.") from exc
    if not 1 <= pages <= max_pages:
        raise RuntimeError(f"ESI lieferte mehr als {max_pages} Seiten.")
    entries = first.json()
    semaphore = asyncio.Semaphore(10)

    async def get_page(page: int) -> list[dict]:
        async with semaphore:
            return await esi.get(path, params={**request_params, "page": page})

    if pages > 1:
        results = await asyncio.gather(*(get_page(page) for page in range(2, pages + 1)))
        for result in results:
            entries.extend(result)
    return entries


def current_account(request: Request) -> int | None:
    value = request.session.get("account_id")
    return value if isinstance(value, int) else None


def require_account(request: Request) -> tuple[int, int]:
    account_id = current_account(request)
    character_id = request.session.get("character_id")
    if account_id is None or not isinstance(character_id, int):
        raise HTTPException(401, "Bitte zuerst mit EVE Online anmelden.")
    if not storage.character_belongs_to(character_id, account_id):
        request.session.clear()
        raise HTTPException(401, "Die Anmeldung ist nicht mehr gültig.")
    return account_id, character_id


def require_admin(request: Request) -> None:
    if not request.session.get("is_admin"):
        raise HTTPException(401, "Bitte als Administrator anmelden.")


def require_module(request: Request, name: str) -> None:
    if not storage.module_allowed(current_account(request), name):
        raise HTTPException(404, "Dieses Modul ist für dich nicht freigeschaltet.")


def render(request: Request, page: str, **context):
    account_id = current_account(request)
    character_id = request.session.get("character_id")
    characters = storage.list_characters(account_id) if account_id else []
    selected_character = next(
        (character for character in characters if character["id"] == character_id), None
    )
    granted_modules = storage.account_module_names(account_id)
    return templates.TemplateResponse(
        request,
        page,
        {
            "app_name": APP_NAME,
            "modules": [
                {**item, "enabled": item["enabled"] or item["name"] in granted_modules}
                for item in storage.list_modules()
            ],
            "active_character": selected_character,
            "characters": characters,
            **context,
        },
    )


@app.get("/")
async def index(request: Request):
    killfeed_error = None
    try:
        kills = await get_kills(
            regions=[
                value.strip()
                for value in storage.get_setting("killfeed_regions").split(",")
                if value.strip()
            ],
            limit=int(storage.get_setting("killfeed_count", "15")),
        )
    except (httpx.HTTPError, ValueError):
        kills = []
        killfeed_error = "Killfeed momentan nicht erreichbar."
    return render(
        request,
        "index.html",
        kills=kills,
        killfeed_error=killfeed_error,
        regions=storage.get_setting("killfeed_regions").split(","),
        map_height=int(storage.get_setting("map_height", "420")),
        map_width=int(storage.get_setting("map_width", "100")),
    )


@app.get("/auth/login")
def login(request: Request, add_alt: bool = False):
    if not os.getenv("SESSION_SECRET"):
        raise HTTPException(503, "SESSION_SECRET muss vor dem EVE-Login gesetzt werden.")
    if add_alt:
        account_id = current_account(request)
        if account_id is None:
            raise HTTPException(401, "Bitte zuerst mit dem Hauptcharakter anmelden.")
        request.session["pending_account_id"] = account_id
    elif current_account(request):
        raise HTTPException(400, "Du bist bereits angemeldet. Nutze „Alt hinzufügen“.")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    try:
        target = authorization_url(state)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return RedirectResponse(target, status_code=302)


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    expected_state = request.session.pop("oauth_state", None)
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        request.session.pop("pending_account_id", None)
        raise HTTPException(400, "Ungültige oder abgelaufene EVE-SSO-Anmeldung.")
    if error:
        request.session.pop("pending_account_id", None)
        raise HTTPException(400, f"EVE-SSO-Anmeldung abgebrochen: {error}")
    if not code:
        raise HTTPException(400, "EVE SSO hat keinen Autorisierungscode zurückgegeben.")

    try:
        identity, token = await exchange_code(code)
        character_id = identity["CharacterID"]
        character_name = identity["CharacterName"]
        if not isinstance(character_id, int) or not isinstance(character_name, str):
            raise ValueError("Ungültige Charakterdaten von EVE SSO.")
        account_id = storage.save_character(
            character_id,
            character_name,
            token,
            request.session.get("pending_account_id"),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        request.session.pop("pending_account_id", None)
        raise HTTPException(502, "EVE-SSO-Anmeldung fehlgeschlagen. Konfiguration und Callback prüfen.") from exc

    request.session.clear()
    request.session["account_id"] = account_id
    request.session["character_id"] = character_id
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/auth/chars")
def characters_page(request: Request):
    account_id, _ = require_account(request)
    return render(request, "characters.html", account_id=account_id)


@app.post("/auth/chars/select")
def select_character(request: Request, character_id: int = Form(...)):
    account_id, _ = require_account(request)
    if not storage.character_belongs_to(character_id, account_id):
        raise HTTPException(404, "Charakter gehört nicht zu diesem Konto.")
    request.session["character_id"] = character_id
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/auth/chars/remove")
def remove_alt(request: Request, character_id: int = Form(...)):
    account_id, current_id = require_account(request)
    if not storage.remove_character(character_id, account_id):
        raise HTTPException(400, "Der Hauptcharakter kann hier nicht entfernt werden.")
    if current_id == character_id:
        characters = storage.list_characters(account_id)
        request.session["character_id"] = characters[0]["id"] if characters else None
    return RedirectResponse("/auth/chars", status_code=303)


@app.post("/auth/chars/delete-account")
def delete_account(request: Request):
    account_id, _ = require_account(request)
    storage.delete_account(account_id)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/dashboard")
async def dashboard(request: Request):
    account_id, character_id = require_account(request)
    require_module(request, "dashboard")
    async def profile(info: dict) -> dict:
        char_id = info["id"]
        async with ESIClient() as esi:
            character_data = await esi.get(f"/characters/{char_id}/")
            corporation_id = character_data.get("corporation_id")
            corporation_data = (
                await esi.get(f"/corporations/{corporation_id}/") if corporation_id else None
            )
            if corporation_data:
                corporation_data["corporation_id"] = corporation_id
            alliance_id = corporation_data.get("alliance_id") if corporation_data else None
            alliance_data = (
                await esi.get(f"/alliances/{alliance_id}/") if alliance_id else None
            )
            if alliance_data:
                alliance_data["alliance_id"] = alliance_id
            ceo_id = corporation_data.get("ceo_id") if corporation_data else None
            ceo = await esi.get(f"/characters/{ceo_id}/") if ceo_id else None
        token_data = storage.get_character(char_id)
        queue_data = []
        total_skill_points = None
        if (
            "esi-skills.read_skillqueue.v1" in token_data["scopes"]
            or "esi-skills.read_skills.v1" in token_data["scopes"]
        ):
            async with ESIClient(char_id) as authenticated_esi:
                if "esi-skills.read_skillqueue.v1" in token_data["scopes"]:
                    queue_data = await authenticated_esi.get(f"/characters/{char_id}/skillqueue/")
                if "esi-skills.read_skills.v1" in token_data["scopes"]:
                    skills_data = await authenticated_esi.get(f"/characters/{char_id}/skills/")
                    total_skill_points = skills_data.get("total_sp", 0)
        return {
            "id": char_id,
            "name": info["name"],
            "is_main": info["is_main"],
            "character": character_data,
            "corporation": corporation_data,
            "alliance": alliance_data,
            "ceo": ceo,
            "queue": queue_data,
            "total_skill_points": total_skill_points,
        }

    try:
        profiles = await asyncio.gather(*[profile(info) for info in storage.list_characters(account_id)])
        selected = next(profile for profile in profiles if profile["id"] == character_id)
        skill_ids = list(
            {
                entry["skill_id"]
                for character_profile in profiles
                for entry in character_profile["queue"]
            }
        )
        if skill_ids:
            async with ESIClient() as esi:
                skill_names_by_id = await resolve_names(esi, set(skill_ids))
            for character_profile in profiles:
                for entry in character_profile["queue"]:
                    entry["skill_name"] = skill_names_by_id.get(
                        entry["skill_id"], f"Skill {entry['skill_id']}"
                    )
    except (httpx.HTTPError, LookupError, StopIteration) as exc:
        raise HTTPException(502, "Charakterdaten sind momentan nicht über ESI erreichbar.") from exc
    return render(
        request,
        "dashboard.html",
        character=selected["character"],
        character_id=character_id,
        selected_profile=selected,
        corporation=selected["corporation"],
        alliance=selected["alliance"],
        ceo=selected["ceo"],
        queue=selected["queue"],
        profiles=profiles,
        motd=storage.get_setting("motd"),
    )


@app.get("/skills")
async def skills_page(request: Request):
    _, character_id = require_account(request)
    require_module(request, "skills")
    character_token = storage.get_character(character_id)
    required = {"esi-skills.read_skills.v1", "esi-skills.read_skillqueue.v1"}
    if not required.issubset(set(character_token["scopes"])):
        return render(
            request,
            "skills.html",
            skills=None,
            queue=[],
            skill_groups=[],
            needs_scopes=True,
        )
    try:
        async with ESIClient(character_id) as esi:
            skills_result, queue = await asyncio.gather(
                esi.get(f"/characters/{character_id}/skills/"),
                esi.get(f"/characters/{character_id}/skillqueue/"),
            )
        skills = skills_result.get("skills", [])
        type_ids = list(
            {skill["skill_id"] for skill in skills}
            | {entry["skill_id"] for entry in queue}
        )
        type_data: dict[int, dict] = {}
        semaphore = asyncio.Semaphore(15)

        async def get_type(type_id: int) -> tuple[int, dict]:
            async with semaphore:
                async with ESIClient() as esi:
                    return type_id, await esi.get(f"/universe/types/{type_id}/")

        type_data.update(await asyncio.gather(*(get_type(type_id) for type_id in type_ids)))
        group_ids = sorted({item["group_id"] for item in type_data.values()})
        async def get_group(group_id: int) -> tuple[int, dict]:
            async with semaphore:
                async with ESIClient() as esi:
                    return group_id, await esi.get(f"/universe/groups/{group_id}/")
        groups_by_id = dict(await asyncio.gather(*(get_group(group_id) for group_id in group_ids)))
        trained = {skill["skill_id"]: skill for skill in skills}
        grouped: dict[int, dict] = {}
        for type_id, item in type_data.items():
            group_id = item["group_id"]
            group = grouped.setdefault(
                group_id,
                {"name": groups_by_id[group_id]["name"], "skills": []},
            )
            group["skills"].append(
                {
                    "id": type_id,
                    "name": item["name"],
                    "level": trained.get(type_id, {}).get("trained_skill_level", 0),
                    "points": trained.get(type_id, {}).get("skillpoints_in_skill", 0),
                }
            )
        skill_groups = sorted(grouped.values(), key=lambda item: item["name"].casefold())
        for group in skill_groups:
            group["skills"].sort(key=lambda item: item["name"].casefold())
        skill_names = {type_id: item["name"] for type_id, item in type_data.items()}
        for entry in queue:
            entry["skill_name"] = skill_names.get(entry["skill_id"], f"Skill {entry['skill_id']}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Skilldaten sind momentan nicht über ESI erreichbar.") from exc
    return render(
        request,
        "skills.html",
        skills=skills_result,
        queue=queue,
        skill_groups=skill_groups,
        needs_scopes=False,
        total_sp=skills_result.get("total_sp", 0),
    )


@app.get("/wallet")
async def wallet_page(request: Request, view: str = "journal"):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    scopes = storage.get_character(character_id)["scopes"]
    if "esi-wallet.read_character_wallet.v1" not in scopes:
        return render(
            request,
            "wallet.html",
            needs_scopes=True,
            view=view,
            wallet=None,
            journal=[],
            transactions=[],
        )
    try:
        async with ESIClient(character_id) as esi:
            balance, journal, transactions = await asyncio.gather(
                esi.get(f"/characters/{character_id}/wallet/"),
                paginated_list(esi, f"/characters/{character_id}/wallet/journal/"),
                esi.get(f"/characters/{character_id}/wallet/transactions/"),
            )
            related_ids = {
                item.get("first_party_id") for item in journal if item.get("first_party_id")
            } | {
                item.get("second_party_id") for item in journal if item.get("second_party_id")
            } | {
                item.get("type_id") for item in transactions if item.get("type_id")
            } | {
                item.get("location_id") for item in transactions if item.get("location_id")
            }
            names = await resolve_names(esi, related_ids)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, "Wallet-Daten sind momentan nicht über ESI erreichbar.") from exc

    since = datetime.now(timezone.utc) - timedelta(days=30)
    recent = []
    for entry in journal:
        try:
            entry_date = datetime.fromisoformat(entry["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if entry_date >= since:
            recent.append(entry)
    income = sum(max(float(item.get("amount", 0)), 0) for item in recent)
    expenses = sum(abs(min(float(item.get("amount", 0)), 0)) for item in recent)
    for entry in journal:
        entry["first_party_name"] = names.get(entry.get("first_party_id"), "Unbekannt")
        entry["second_party_name"] = names.get(entry.get("second_party_id"), "Unbekannt")
        entry["amount"] = float(entry.get("amount", 0))
    for item in transactions:
        item["type_name"] = names.get(item.get("type_id"), str(item.get("type_id", "Unbekannt")))
        item["location_name"] = names.get(
            item.get("location_id"), str(item.get("location_id", "Unbekannt"))
        )
    return render(
        request,
        "wallet.html",
        needs_scopes=False,
        view=view if view in {"journal", "transactions"} else "journal",
        wallet=float(balance),
        journal=journal[:100],
        transactions=transactions[:100],
        income_30=income,
        expenses_30=expenses,
        net_30=income - expenses,
    )


@app.get("/wallet/distribution")
async def wallet_distribution(request: Request):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if "esi-wallet.read_character_wallet.v1" not in storage.get_character(character_id)["scopes"]:
        return render(
            request,
            "wallet_distribution.html",
            needs_scopes=True,
            plan={"start_balance": 0, "labels": [], "entries": []},
            wallet_balance=0,
            total_pool=0,
            total_expenses=0,
            allocations=[],
            unallocated=100,
        )
    plan = storage.wallet_plan(account_id, character_id)
    try:
        async with ESIClient(character_id) as esi:
            wallet_balance = float(
                await esi.get(f"/characters/{character_id}/wallet/")
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Wallet-Saldo ist momentan nicht über ESI erreichbar.") from exc
    total_income = sum(entry["amount"] for entry in plan["entries"] if entry["kind"] == "income")
    total_expenses = sum(entry["amount"] for entry in plan["entries"] if entry["kind"] == "expense")
    total_pool = plan["start_balance"] + total_income
    percentages = sum(label["percentage"] for label in plan["labels"])
    spent_by_label = {
        label["id"]: sum(
            entry["amount"]
            for entry in plan["entries"]
            if entry["kind"] == "expense" and entry["label_id"] == label["id"]
        )
        for label in plan["labels"]
    }
    allocations = [
        {
            **label,
            "allocated": total_pool * label["percentage"] / 100,
            "spent": spent_by_label[label["id"]],
            "remaining": total_pool * label["percentage"] / 100 - spent_by_label[label["id"]],
        }
        for label in plan["labels"]
    ]
    return render(
        request,
        "wallet_distribution.html",
        needs_scopes=False,
        plan=plan,
        wallet_balance=wallet_balance,
        total_pool=total_pool,
        total_expenses=total_expenses,
        allocations=allocations,
        unallocated=max(0, 100 - percentages),
    )


@app.post("/wallet/plan/start")
async def save_wallet_plan_start(request: Request):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if "esi-wallet.read_character_wallet.v1" not in storage.get_character(character_id)["scopes"]:
        raise HTTPException(403, "Wallet-ESI-Scope fehlt.")
    try:
        async with ESIClient(character_id) as esi:
            balance = float(await esi.get(f"/characters/{character_id}/wallet/"))
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Wallet-Saldo ist momentan nicht über ESI erreichbar.") from exc
    storage.save_wallet_start(account_id, character_id, balance)
    return RedirectResponse("/wallet/distribution", status_code=303)


@app.post("/wallet/plan/labels")
def create_wallet_label(
    request: Request,
    name: str = Form(...),
    percentage: float = Form(...),
):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if (
        not name.strip()
        or len(name) > 80
        or not math.isfinite(percentage)
        or not 0 <= percentage <= 100
    ):
        raise HTTPException(400, "Beschriftung oder Prozentwert ist ungültig.")
    plan = storage.wallet_plan(account_id, character_id)
    total = sum(label["percentage"] for label in plan["labels"]) + percentage
    if total > 100:
        raise HTTPException(400, "Die Summe der Beschriftungen darf 100 % nicht überschreiten.")
    try:
        storage.add_wallet_label(account_id, character_id, name.strip(), percentage)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Diese Beschriftung ist bereits vorhanden.") from exc
    return RedirectResponse("/wallet/distribution", status_code=303)


@app.post("/wallet/plan/labels/{label_id}/delete")
def delete_wallet_label(request: Request, label_id: int):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if not storage.remove_wallet_label(account_id, character_id, label_id):
        raise HTTPException(404, "Beschriftung nicht gefunden.")
    return RedirectResponse("/wallet/distribution", status_code=303)


@app.post("/wallet/plan/entries")
def create_wallet_entry(
    request: Request,
    kind: str = Form(...),
    description: str = Form(...),
    amount: float = Form(...),
    entry_date: str = Form(...),
    label_id: int | None = Form(None),
):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if kind not in {"income", "expense"} or not description.strip() or len(description) > 120:
        raise HTTPException(400, "Eintragstyp oder Beschreibung ist ungültig.")
    if not math.isfinite(amount) or amount <= 0 or amount > 1e15:
        raise HTTPException(400, "Betrag muss größer als 0 sein.")
    try:
        date.fromisoformat(entry_date)
        storage.add_wallet_entry(
            account_id,
            character_id,
            kind,
            label_id if kind == "expense" else None,
            description.strip(),
            amount,
            entry_date,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/wallet/distribution", status_code=303)


@app.post("/wallet/plan/entries/{entry_id}/delete")
def delete_wallet_entry(request: Request, entry_id: int):
    account_id, character_id = require_account(request)
    require_module(request, "wallet")
    if not storage.remove_wallet_entry(account_id, character_id, entry_id):
        raise HTTPException(404, "Wallet-Eintrag nicht gefunden.")
    return RedirectResponse("/wallet/distribution", status_code=303)


def marketwatcher_context(account_id: int, watchlist_id: int | None):
    lists = storage.list_watchlists(account_id)
    if watchlist_id is None or not any(item["id"] == watchlist_id for item in lists):
        watchlist_id = lists[0]["id"]
    watchlist = storage.get_watchlist(account_id, watchlist_id)
    if watchlist is None:
        raise HTTPException(404, "Liste nicht gefunden.")
    locations = storage.list_locations()
    return lists, watchlist, locations


async def refresh_marketwatcher(watchlist: dict, locations: list[dict]) -> None:
    selected_locations = [
        location
        for location in locations
        if location["id"] in watchlist["location_ids"]
    ]
    semaphore = asyncio.Semaphore(5)

    async def update(item: dict, location: dict) -> None:
        async with semaphore:
            result = await get_market_quote(location, item["type_id"])
        storage.record_prices(
            watchlist["id"],
            item["type_id"],
            location["id"],
            result["lowest_sell"],
            result["highest_buy"],
        )

    await asyncio.gather(
        *[
            update(item, location)
            for item in watchlist["items"]
            for location in selected_locations
        ]
    )


@app.get("/marketwatcher")
async def marketwatcher_page(request: Request, watchlist_id: int | None = None):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    lists, watchlist, locations = marketwatcher_context(account_id, watchlist_id)
    prices = storage.watchlist_prices(watchlist["id"])
    by_item: dict[int, dict[int, dict]] = {}
    for row in prices:
        by_item.setdefault(row["item_id"], {})[row["location_pk"]] = row
    latest = max((row["sampled_at"] for row in prices if row["sampled_at"]), default=None)
    return render(
        request,
        "marketwatcher.html",
        watchlists=lists,
        watchlist=watchlist,
        locations=locations,
        prices=by_item,
        last_updated=latest,
    )


@app.post("/marketwatcher/lists")
def create_marketwatcher_list(request: Request, name: str = Form(...)):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    name = name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "Listenname muss zwischen 1 und 60 Zeichen lang sein.")
    try:
        list_id = storage.create_watchlist(account_id, name)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Eine Liste mit diesem Namen existiert bereits.") from exc
    return RedirectResponse(f"/marketwatcher?watchlist_id={list_id}", status_code=303)


@app.post("/marketwatcher/lists/{watchlist_id}/delete")
def remove_marketwatcher_list(request: Request, watchlist_id: int):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    if not storage.delete_watchlist(account_id, watchlist_id):
        raise HTTPException(404, "Liste nicht gefunden.")
    return RedirectResponse("/marketwatcher", status_code=303)


@app.post("/marketwatcher/locations")
def select_marketwatcher_locations(
    request: Request,
    watchlist_id: int = Form(...),
    location_ids: list[int] = Form([]),
):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    if not storage.get_watchlist(account_id, watchlist_id):
        raise HTTPException(404, "Liste nicht gefunden.")
    try:
        storage.set_watchlist_locations(watchlist_id, location_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/marketwatcher?watchlist_id={watchlist_id}", status_code=303)


@app.post("/marketwatcher/items")
async def add_marketwatcher_item(
    request: Request,
    watchlist_id: int = Form(...),
    name: str = Form(...),
    quantity: int = Form(1),
):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    if not storage.get_watchlist(account_id, watchlist_id):
        raise HTTPException(404, "Liste nicht gefunden.")
    if not name.strip() or len(name) > 120 or not 1 <= quantity <= 1_000_000:
        raise HTTPException(400, "Itemname oder Menge ist ungültig.")
    try:
        types = await resolve_type_ids([name.strip()])
    except httpx.HTTPError as exc:
        raise HTTPException(502, "EVE-Item konnte momentan nicht über ESI gesucht werden.") from exc
    type_id = types.get(name.strip())
    if type_id is None:
        raise HTTPException(404, f"Kein EVE-Item mit dem exakten Namen „{name.strip()}“ gefunden.")
    storage.add_watchlist_item(watchlist_id, type_id, name.strip(), quantity)
    return RedirectResponse(f"/marketwatcher?watchlist_id={watchlist_id}", status_code=303)


@app.post("/marketwatcher/import")
async def import_marketwatcher_fitting(
    request: Request,
    watchlist_id: int = Form(...),
    fitting: str = Form(...),
):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    if not storage.get_watchlist(account_id, watchlist_id):
        raise HTTPException(404, "Liste nicht gefunden.")
    if len(fitting) > 100_000:
        raise HTTPException(413, "Das EFT-Fitting ist zu groß.")
    counts: Counter[str] = Counter()
    for line in fitting.splitlines():
        value = line.strip()
        if (
            not value
            or value.startswith(("[", "#"))
            or value.casefold() in {"drones in bay", "cargo", "cargo bay", "fighters"}
        ):
            continue
        quantity = 1
        match = re.search(r"\s+[x×]\s*(\d+)$", value, re.IGNORECASE)
        if match:
            quantity = int(match.group(1))
            value = value[: match.start()].strip()
        if not 1 <= quantity <= 1_000_000:
            raise HTTPException(400, "Eine EFT-Itemmenge liegt außerhalb des erlaubten Bereichs.")
        item_names = [part.strip() for part in value.split(",", 1)]
        for index, item_name in enumerate(item_names):
            if item_name and not item_name.isdecimal():
                counts[item_name] += quantity if index == len(item_names) - 1 else 1
    if not counts or len(counts) > 100:
        raise HTTPException(400, "Das EFT-Fitting enthält keine oder zu viele Items.")
    try:
        type_ids = await resolve_type_ids(list(counts))
    except httpx.HTTPError as exc:
        raise HTTPException(502, "EFT-Items konnten momentan nicht über ESI gesucht werden.") from exc
    missing = [name for name in counts if name not in type_ids]
    if missing:
        raise HTTPException(422, f"Nicht in EVE gefunden: {', '.join(missing[:10])}")
    for name, quantity in counts.items():
        storage.add_watchlist_item(watchlist_id, type_ids[name], name, quantity)
    return RedirectResponse(f"/marketwatcher?watchlist_id={watchlist_id}", status_code=303)


@app.post("/marketwatcher/items/{item_id}/delete")
def delete_marketwatcher_item(request: Request, item_id: int, watchlist_id: int = Form(...)):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    if not storage.get_watchlist(account_id, watchlist_id):
        raise HTTPException(404, "Liste nicht gefunden.")
    if not storage.remove_watchlist_item(watchlist_id, item_id):
        raise HTTPException(404, "Item nicht gefunden.")
    return RedirectResponse(f"/marketwatcher?watchlist_id={watchlist_id}", status_code=303)


@app.post("/marketwatcher/refresh")
async def refresh_marketwatcher_route(
    request: Request,
    watchlist_id: int = Form(...),
):
    account_id, _ = require_account(request)
    require_module(request, "marketwatcher")
    _, watchlist, locations = marketwatcher_context(account_id, watchlist_id)
    try:
        await refresh_marketwatcher(watchlist, locations)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Mindestens ein Marktstandort konnte nicht abgefragt werden.") from exc
    except ValueError as exc:
        raise HTTPException(502, "Marktdaten konnten nicht vollständig gelesen werden.") from exc
    return RedirectResponse(f"/marketwatcher?watchlist_id={watchlist_id}", status_code=303)


async def account_assets(account_id: int) -> tuple[dict[int, int], list[str]]:
    characters = storage.list_characters(account_id)
    stock: dict[int, int] = {}
    missing: list[str] = []
    for character in characters:
        if "esi-assets.read_assets.v1" not in character["scopes"]:
            missing.append(character["name"])
            continue
        async with ESIClient(character["id"]) as esi:
            response = await esi.request(
                "GET",
                f"/characters/{character['id']}/assets/",
                params={"page": 1},
            )
            assets = response.json()
            try:
                pages = max(1, int(response.headers.get("X-Pages", "1")))
            except ValueError as exc:
                raise ValueError("ESI lieferte eine ungültige Asset-Seitenanzahl.") from exc
            if pages > 50:
                raise ValueError("Der Assetbestand enthält mehr als 50 ESI-Seiten.")
            if pages > 1:
                others = await asyncio.gather(
                    *[
                        esi.get(
                            f"/characters/{character['id']}/assets/",
                            params={"page": page},
                        )
                        for page in range(2, pages + 1)
                    ]
                )
                for page in others:
                    assets.extend(page)
        for asset in assets:
            stock[asset["type_id"]] = stock.get(asset["type_id"], 0) + int(
                asset.get("quantity", 0)
            )
    return stock, missing


async def character_skill_levels(character_id: int) -> dict[int, int] | None:
    character = storage.get_character(character_id)
    if not character or "esi-skills.read_skills.v1" not in character["scopes"]:
        return None
    async with ESIClient(character_id) as esi:
        data = await esi.get(f"/characters/{character_id}/skills/")
    return {
        item["skill_id"]: int(item.get("active_skill_level", 0))
        for item in data.get("skills", [])
    }


@app.get("/industry")
def industry_page(request: Request):
    require_account(request)
    require_module(request, "industry")
    return render(
        request,
        "industry.html",
        result=None,
        form_data={},
        error=None,
        stock_warning=None,
        skill_note=None,
        decryptors=list(DECRYPTORS),
    )


@app.post("/industry/calculate")
async def calculate_industry(
    request: Request,
    product_name: str = Form(...),
    runs: int = Form(1),
    production_type: str = Form("manufacturing"),
    product_me: float = Form(0),
    product_te: float = Form(0),
    component_me: float = Form(0),
    component_te: float = Form(0),
    structure_material_bonus: float = Form(0),
    structure_time_bonus: float = Form(0),
    rig_material_bonus: float = Form(0),
    rig_time_bonus: float = Form(0),
    system_cost_index: float = Form(0),
    cost_bonus: float = Form(0),
    tax_rate: float = Form(0.25),
    security: str = Form("high"),
    build_components: bool = Form(False),
    include_stock: bool = Form(False),
    skill_mode: str = Form("character"),
    invention: bool = Form(False),
    decryptor: str = Form(""),
):
    account_id, character_id = require_account(request)
    require_module(request, "industry")
    form_data = {
        "product_name": product_name,
        "runs": runs,
        "production_type": production_type,
        "product_me": product_me,
        "product_te": product_te,
        "component_me": component_me,
        "component_te": component_te,
        "structure_material_bonus": structure_material_bonus,
        "structure_time_bonus": structure_time_bonus,
        "rig_material_bonus": rig_material_bonus,
        "rig_time_bonus": rig_time_bonus,
        "system_cost_index": system_cost_index,
        "cost_bonus": cost_bonus,
        "tax_rate": tax_rate,
        "security": security,
        "build_components": build_components,
        "include_stock": include_stock,
        "skill_mode": skill_mode,
        "invention": invention,
        "decryptor": decryptor,
    }
    percentages = (
        product_me,
        product_te,
        component_me,
        component_te,
        structure_material_bonus,
        structure_time_bonus,
        rig_material_bonus,
        rig_time_bonus,
        system_cost_index,
        cost_bonus,
        tax_rate,
    )
    if not product_name.strip() or len(product_name) > 120 or not 1 <= runs <= 10000:
        raise HTTPException(400, "Produkt oder Laufanzahl ist ungültig.")
    if not all(math.isfinite(value) for value in percentages):
        raise HTTPException(400, "Bonus- und Kostenwerte müssen gültige Zahlen sein.")
    if any(value < 0 or value > 100 for value in percentages):
        raise HTTPException(400, "Prozentwerte müssen zwischen 0 und 100 liegen.")
    if skill_mode not in {"character", "max", "none"} or (
        decryptor and decryptor not in DECRYPTORS
    ):
        raise HTTPException(400, "Skill-Modus oder Decryptor ist ungültig.")
    if production_type not in {"manufacturing", "reaction"} or security not in {
        "high",
        "low",
        "null",
    }:
        raise HTTPException(400, "Produktionsart oder Sicherheitsklasse ist ungültig.")
    try:
        ids = await resolve_type_ids([product_name.strip()])
        product_id = ids.get(product_name.strip())
        if product_id is None:
            raise HTTPException(404, "EVE-Produkt mit diesem exakten Namen wurde nicht gefunden.")
        types, group_names = await type_catalog()
        recipes = await manufacturing_recipes()
        activity_id = 9 if production_type == "reaction" else 1
        if not any(item["activity_id"] == activity_id for item in recipes.get(product_id, [])):
            raise HTTPException(404, "Für dieses Produkt wurde kein passendes SDE-Rezept gefunden.")
        security_factor = {"high": 0.5, "low": 0.75, "null": 1.0}[security]
        skill_note = None
        if skill_mode == "max":
            levels = {skill_id: 5 for skill_id in types}
        elif skill_mode == "none":
            levels = {}
        else:
            levels = await character_skill_levels(character_id)
            if levels is None:
                levels = {}
                skill_note = (
                    "Dem gewählten Charakter fehlt esi-skills.read_skills.v1; "
                    "Skills werden als Level 0 angenommen."
                )
        invention_plan = None
        if invention:
            if activity_id != 1:
                raise HTTPException(400, "Erfindung ist nur bei Herstellung möglich.")
            blueprint_ids = {
                item["blueprint_id"]
                for item in recipes[product_id]
                if item["activity_id"] == 1
            }
            options = await invention_recipes()
            option = next(
                (
                    options[blueprint_id][0]
                    for blueprint_id in sorted(blueprint_ids)
                    if blueprint_id in options
                ),
                None,
            )
            if option is None:
                raise HTTPException(404, "Für dieses Produkt gibt es keine Erfindung (kein T2-Blueprint).")
            invention_plan = plan_invention(option, levels, decryptor, types)
            product_me, product_te = invention_plan["me"], invention_plan["te"]
            form_data["product_me"], form_data["product_te"] = product_me, product_te
        result = create_plan(
            product_id,
            runs,
            recipes,
            types,
            group_names,
            activity_id,
            build_components,
            product_me,
            product_te,
            component_me,
            component_te,
            structure_material_bonus,
            structure_time_bonus,
            rig_material_bonus,
            rig_time_bonus,
            security_factor,
            system_cost_index,
            cost_bonus,
            tax_rate,
            {},
            skill_time_factor(activity_id, levels),
        )
        result["skill_time_factor"] = skill_time_factor(activity_id, levels)
        jita = next(item for item in storage.list_locations() if item["is_default"])
        price_type_ids = {item["type_id"] for item in result["materials"]}
        price_type_ids.add(product_id)
        decryptor_id = None
        if invention_plan:
            if decryptor:
                decryptor_id = next(
                    (
                        type_id
                        for type_id, row in types.items()
                        if field(row, "typeName") == decryptor
                    ),
                    None,
                )
                if decryptor_id is None:
                    raise ValueError("Decryptor nicht in der SDE gefunden.")
                price_type_ids.add(decryptor_id)
            price_type_ids.update(item["type_id"] for item in invention_plan["materials"])
        adjusted_type_ids = price_type_ids | {
            item["type_id"] for item in result["eiv_items"]
        }
        adjusted = await get_adjusted_prices(adjusted_type_ids)
        for item in result["eiv_items"]:
            item["adjusted_price"] = adjusted.get(item["type_id"])
        if all(item["adjusted_price"] is not None for item in result["eiv_items"]):
            eiv = sum(
                item["quantity"] * item["adjusted_price"]
                for item in result["eiv_items"]
            )
            result["job_cost"] = eiv * (
                system_cost_index / 100 * (1 - cost_bonus / 100)
                + tax_rate / 100
                + 0.04
            )
        semaphore = asyncio.Semaphore(8)

        async def priced(type_id: int):
            async with semaphore:
                return type_id, await get_market_quote(jita, type_id)

        quotes = dict(
            await asyncio.gather(*(priced(type_id) for type_id in price_type_ids))
        )
        stock, missing_stock_scopes = ({}, [])
        if include_stock:
            stock, missing_stock_scopes = await account_assets(account_id)
        result = apply_market_and_stock(result, quotes, stock, adjusted)
        if invention_plan:
            product_recipe = next(
                item for item in recipes[product_id] if item["activity_id"] == 1
            )
            names = {
                type_id: field(row, "typeName", default=f"Item {type_id}")
                for type_id, row in types.items()
                if type_id in price_type_ids
            }
            result = apply_invention(
                result,
                invention_plan,
                names,
                quotes,
                adjusted,
                product_recipe["materials"],
                decryptor_id,
                system_cost_index,
                cost_bonus,
                tax_rate,
            )
        result["steps"].sort(key=lambda step: step["depth"])
        result["sequential_seconds"] = int(result["sequential_seconds"])
        result["critical_seconds"] = int(result["critical_seconds"])
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            502,
            "Produktionsplan konnte nicht berechnet werden. SDE-/ESI-Erreichbarkeit und Datensatz prüfen.",
        ) from exc
    stock_warning = (
        "Asset-Scope fehlt bei: " + ", ".join(missing_stock_scopes)
        if missing_stock_scopes
        else None
    )
    return render(
        request,
        "industry.html",
        result=result,
        form_data=form_data,
        error=None,
        stock_warning=stock_warning,
        skill_note=skill_note,
        decryptors=list(DECRYPTORS),
    )


@app.get("/planetary-industry")
def planetary_industry_page(request: Request):
    require_account(request)
    require_module(request, "planetary-industry")
    locations = storage.list_locations()
    return render(
        request,
        "planetary_industry.html",
        result=None,
        form_data={
            "location_ids": [
                location["id"] for location in locations if location["is_default"]
            ]
        },
        error=None,
        locations=locations,
    )


@app.post("/planetary-industry/calculate")
async def calculate_planetary_industry(
    request: Request,
    product_name: str = Form(...),
    quantity: int = Form(1),
    buy_level: int = Form(1),
    location_ids: list[int] = Form([]),
):
    require_account(request)
    require_module(request, "planetary-industry")
    form_data = {
        "product_name": product_name,
        "quantity": quantity,
        "buy_level": buy_level,
        "location_ids": location_ids,
    }
    if not product_name.strip() or len(product_name) > 120 or not 1 <= quantity <= 1_000_000:
        raise HTTPException(400, "PI-Produkt oder Menge ist ungültig.")
    if not 0 <= buy_level <= 3:
        raise HTTPException(400, "Einkaufsstufe muss zwischen P0 und P3 liegen.")
    locations = storage.list_locations()
    selected_ids = set(location_ids)
    if not selected_ids:
        selected_ids = {next(item["id"] for item in locations if item["is_default"])}
    form_data["location_ids"] = sorted(selected_ids)
    selected_locations = [
        location for location in locations if location["id"] in selected_ids
    ]
    if len(selected_locations) != len(selected_ids):
        raise HTTPException(400, "Mindestens ein gewählter Standort wurde nicht gefunden.")
    try:
        ids = await resolve_type_ids([product_name.strip()])
        product_id = ids.get(product_name.strip())
        if product_id is None:
            raise HTTPException(404, "PI-Produkt mit diesem exakten Namen wurde nicht gefunden.")
        types, group_names = await type_catalog()
        recipes = await planetary_recipes()
        if product_id not in recipes:
            raise HTTPException(404, "Für dieses Produkt wurde kein PI-Schema gefunden.")
        plan = create_planetary_plan(
            product_id,
            quantity,
            buy_level,
            recipes,
            types,
            group_names,
        )
        type_ids = {item["type_id"] for item in plan["tree"]}
        type_ids.add(product_id)
        semaphore = asyncio.Semaphore(8)

        async def quote_at(location: dict, type_id: int) -> tuple[int, dict]:
            async with semaphore:
                return type_id, await get_market_quote(location, type_id)

        location_results = []
        for location in selected_locations:
            quotes = dict(
                await asyncio.gather(
                    *(quote_at(location, type_id) for type_id in type_ids)
                )
            )
            summary = add_location_prices(plan, quotes)
            location_results.append(
                {
                    "location": location,
                    "summary": summary,
                    "quotes": quotes,
                }
            )
    except HTTPException:
        raise
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            502,
            "PI-Produktionskette konnte nicht berechnet werden. SDE-/ESI-Erreichbarkeit und Datensatz prüfen.",
        ) from exc
    return render(
        request,
        "planetary_industry.html",
        result={"plan": plan, "locations": location_results},
        form_data=form_data,
        error=None,
        locations=locations,
    )


@app.get("/admin/login")
def admin_login_page(request: Request):
    if request.session.get("is_admin"):
        return RedirectResponse("/admin", status_code=303)
    return render(
        request,
        "admin_login.html",
        error=None,
        oidc_enabled=bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET),
    )


@app.get("/admin/oidc/login")
async def admin_oidc_login(request: Request):
    if not OIDC_ISSUER or not OIDC_CLIENT_ID or not OIDC_CLIENT_SECRET:
        raise HTTPException(503, "Authentik/OIDC ist nicht vollständig konfiguriert.")
    if not OIDC_ISSUER.startswith("https://"):
        raise HTTPException(503, "OIDC_ISSUER muss HTTPS verwenden.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{OIDC_ISSUER}/.well-known/openid-configuration")
            response.raise_for_status()
            discovery = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Authentik-OIDC-Konfiguration konnte nicht geladen werden.") from exc
    if discovery.get("issuer", "").rstrip("/") != OIDC_ISSUER:
        raise HTTPException(502, "Authentik hat einen abweichenden OIDC-Issuer gemeldet.")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    redirect_uri = f"{os.getenv('PUBLIC_URL', '').rstrip('/')}/admin/oidc/callback"
    query = urlencode(
        {
            "response_type": "code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
        }
    )
    return RedirectResponse(f"{discovery['authorization_endpoint']}?{query}", status_code=302)


@app.get("/admin/oidc/callback")
async def admin_oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    expected_state = request.session.pop("oidc_state", None)
    expected_nonce = request.session.pop("oidc_nonce", None)
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        raise HTTPException(400, "Ungültige oder abgelaufene OIDC-Anmeldung.")
    if error or not code:
        raise HTTPException(400, "Authentik hat die Anmeldung abgebrochen.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            discovery_response = await client.get(
                f"{OIDC_ISSUER}/.well-known/openid-configuration"
            )
            discovery_response.raise_for_status()
            discovery = discovery_response.json()
            token_response = await client.post(
                discovery["token_endpoint"],
                auth=(OIDC_CLIENT_ID, OIDC_CLIENT_SECRET),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{os.getenv('PUBLIC_URL', '').rstrip('/')}/admin/oidc/callback",
                },
            )
            token_response.raise_for_status()
            id_token = token_response.json()["id_token"]
            jwks_response = await client.get(discovery["jwks_uri"])
            jwks_response.raise_for_status()
            jwks = jwks_response.json()["keys"]
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") not in {"RS256", "ES256"}:
            raise jwt.InvalidTokenError("Unsupported signing algorithm")
        key_data = next(key for key in jwks if key.get("kid") == header.get("kid"))
        signing_key = jwt.PyJWK.from_dict(key_data).key
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256", "ES256"],
            audience=OIDC_CLIENT_ID,
            issuer=discovery["issuer"],
        )
    except (httpx.HTTPError, KeyError, ValueError, StopIteration, jwt.PyJWTError) as exc:
        raise HTTPException(502, "Authentik-OIDC-Anmeldung konnte nicht verifiziert werden.") from exc
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not expected_nonce or not hmac.compare_digest(nonce, expected_nonce):
        raise HTTPException(403, "Authentik-OIDC-Nonce stimmt nicht überein.")
    required_group = os.getenv("OIDC_ADMIN_GROUP")
    groups = claims.get("groups", [])
    if required_group and (not isinstance(groups, list) or required_group not in groups):
        raise HTTPException(403, "Dein Authentik-Konto ist nicht in der Admin-Gruppe.")
    request.session["is_admin"] = True
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected or expected == "bitte-aendern" or not hmac.compare_digest(password, expected):
        return render(
            request,
            "admin_login.html",
            error="Anmeldung fehlgeschlagen.",
            oidc_enabled=bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET),
        )
    request.session["is_admin"] = True
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse("/", status_code=303)


@app.get("/admin")
def admin(request: Request):
    require_admin(request)
    settings = {
        key: storage.get_setting(key)
        for key in ("motd", "map_height", "map_width", "killfeed_regions", "killfeed_count")
    }
    return render(
        request,
        "admin.html",
        settings=settings,
        module_details=MODULE_DETAILS,
        locations=storage.list_locations(),
        modules=storage.list_modules(),
        account_access=storage.list_accounts_with_modules(),
        error=request.query_params.get("error"),
    )


@app.post("/admin/settings")
def update_settings(
    request: Request,
    motd: str = Form(""),
    map_height: int = Form(420),
    map_width: int = Form(100),
    killfeed_regions: str = Form(""),
    killfeed_count: int = Form(15),
):
    require_admin(request)
    if len(motd) > 1000 or not 250 <= map_height <= 1200 or not 40 <= map_width <= 100:
        raise HTTPException(400, "Einstellung außerhalb des erlaubten Wertebereichs.")
    if not 5 <= killfeed_count <= 50:
        raise HTTPException(400, "Die Anzahl der Killfeed-Einträge muss zwischen 5 und 50 liegen.")
    for key, value in {
        "motd": motd.strip(),
        "map_height": str(map_height),
        "map_width": str(map_width),
        "killfeed_regions": ",".join(
            item.strip() for item in killfeed_regions.split(",") if item.strip()
        ),
        "killfeed_count": str(killfeed_count),
    }.items():
        storage.set_setting(key, value)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/modules")
def update_modules(request: Request, enabled: list[str] = Form([])):
    require_admin(request)
    unknown = set(enabled) - set(MODULE_DETAILS)
    if unknown:
        raise HTTPException(400, "Unbekanntes Modul.")
    for name in MODULE_DETAILS:
        storage.set_module(name, name in enabled)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/accounts/{account_id}/modules")
def update_account_modules(
    request: Request, account_id: int, modules: list[str] = Form([])
):
    require_admin(request)
    if set(modules) - set(MODULE_DETAILS):
        raise HTTPException(400, "Unbekanntes Modul.")
    if not storage.set_account_modules(account_id, set(modules)):
        raise HTTPException(404, "Account nicht gefunden.")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/locations")
async def create_location(
    request: Request,
    name: str = Form(...),
    location_id: str = Form(...),
    kind: str = Form(...),
    character_id: str = Form(""),
):
    require_admin(request)
    if kind not in {"station", "structure"} or not name.strip():
        raise HTTPException(400, "Standortdaten sind ungültig.")
    identifier = location_id.strip()
    if identifier.isdecimal():
        resolved_id = int(identifier)
    elif kind == "station":
        try:
            async with ESIClient() as esi:
                matches = await esi.post("/universe/ids/", json=[identifier])
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Stationsname konnte nicht über ESI aufgelöst werden.") from exc
        candidates = [
            item
            for item in matches.get("stations", [])
            if item["name"].casefold() == identifier.casefold()
        ]
        if len(candidates) != 1:
            raise HTTPException(404, "NPC-Station mit diesem exakten Namen wurde nicht gefunden.")
        resolved_id = candidates[0]["id"]
    else:
        raise HTTPException(400, "Eine Structure muss mit ihrer numerischen ID eingetragen werden.")
    if resolved_id < 1:
        raise HTTPException(400, "Stations-/Structure-ID ist ungültig.")
    if kind == "station":
        character_id = ""
    elif not character_id.strip().isdigit() or not storage.get_character(int(character_id)):
        raise HTTPException(400, "Für eine Structure muss ein angemeldeter Charakter gewählt werden.")
    try:
        storage.add_location(
            name.strip(), resolved_id, kind, int(character_id) if character_id else None
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Diese Stations-/Structure-ID ist bereits eingetragen.") from exc
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/locations/{location_pk}/delete")
def remove_location(request: Request, location_pk: int):
    require_admin(request)
    if not storage.delete_location(location_pk):
        raise HTTPException(404, "Standort nicht gefunden.")
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/kills")
async def kills(regions: str | None = None):
    selected_regions = (
        [value.strip() for value in regions.split(",") if value.strip()]
        if regions is not None
        else [
            value.strip()
            for value in storage.get_setting("killfeed_regions").split(",")
            if value.strip()
        ]
    )
    try:
        return await get_kills(
            regions=selected_regions,
            limit=int(storage.get_setting("killfeed_count", "15")),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Killfeed momentan nicht erreichbar.") from exc


@app.get("/api/map")
async def map_api():
    try:
        return await get_map_data()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Kartendaten momentan nicht erreichbar.") from exc


@app.get("/incursions")
async def incursions_page(request: Request):
    require_module(request, "incursions")
    try:
        data = await get_incursions()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Incursion-Daten sind momentan nicht über ESI erreichbar.") from exc
    return render(request, "incursions.html", incursions=data)


@app.get("/api/incursions")
async def incursions_api():
    require_module(request, "incursions")
    try:
        return await get_incursions()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Incursion-Daten sind momentan nicht über ESI erreichbar.") from exc


@app.get("/health")
def health():
    return {"status": "ok"}
