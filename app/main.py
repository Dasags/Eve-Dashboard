import os
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.services.killfeed import get_kills

APP_NAME = os.getenv("APP_NAME", "EVE-World")
EVE_SSO = "https://login.eveonline.com"
ESI = "https://esi.evetech.net/latest"
ESI_HEADERS = {
    "User-Agent": f"{APP_NAME} ({os.getenv('CONTACT', 'configure CONTACT')})",
}

app = FastAPI(title=APP_NAME)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(64),
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "true").lower() == "true",
)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _oauth_config() -> tuple[str, str, str]:
    client_id = os.getenv("EVE_CLIENT_ID")
    client_secret = os.getenv("EVE_CLIENT_SECRET")
    public_url = os.getenv("PUBLIC_URL")
    if not os.getenv("SESSION_SECRET"):
        raise HTTPException(503, "SESSION_SECRET muss für den EVE-Login gesetzt sein.")
    if not client_id or not client_secret:
        raise HTTPException(503, "EVE_CLIENT_ID und EVE_CLIENT_SECRET sind nicht konfiguriert.")
    parsed_url = urlsplit(public_url or "")
    is_https = parsed_url.scheme == "https" and bool(parsed_url.hostname)
    is_local_http = (
        parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1"}
    )
    if (
        not public_url
        or not (is_https or is_local_http)
        or parsed_url.path not in ("", "/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise HTTPException(503, "PUBLIC_URL muss auf die öffentliche Dashboard-Adresse zeigen.")
    return client_id, client_secret, f"{public_url.rstrip('/')}/auth/callback"


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": APP_NAME,
            "character_name": request.session.get("character_name"),
        },
    )


@app.get("/auth/login")
def login(request: Request):
    client_id, _, redirect_uri = _oauth_config()
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    query = urlencode(
        {
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "state": state,
        }
    )
    return RedirectResponse(f"{EVE_SSO}/v2/oauth/authorize?{query}")


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    expected_state = request.session.pop("oauth_state", None)
    if error:
        raise HTTPException(400, f"EVE-SSO-Anmeldung abgebrochen: {error}")
    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(400, "Ungültige oder abgelaufene EVE-SSO-Anmeldung.")

    client_id, client_secret, redirect_uri = _oauth_config()
    try:
        async with httpx.AsyncClient(timeout=15, headers=ESI_HEADERS) as client:
            token_response = await client.post(
                f"{EVE_SSO}/v2/oauth/token",
                auth=(client_id, client_secret),
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            identity_response = await client.get(
                f"{EVE_SSO}/oauth/verify",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            identity_response.raise_for_status()
            identity = identity_response.json()
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(502, "Die Anmeldung bei EVE SSO ist fehlgeschlagen.") from exc

    character_id = identity.get("CharacterID")
    character_name = identity.get("CharacterName")
    if not isinstance(character_id, int) or not isinstance(character_name, str):
        raise HTTPException(502, "EVE SSO hat keine gültige Charakter-Identität zurückgegeben.")

    request.session.clear()
    request.session["character_id"] = character_id
    request.session["character_name"] = character_name
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard")
async def dashboard(request: Request):
    character_id = request.session.get("character_id")
    if not character_id:
        return RedirectResponse("/", status_code=303)

    try:
        async with httpx.AsyncClient(timeout=15, headers=ESI_HEADERS) as client:
            response = await client.get(f"{ESI}/characters/{character_id}/")
            response.raise_for_status()
            character = response.json()
            corporation_id = character.get("corporation_id")
            corporation = None
            alliance = None
            if corporation_id:
                corp_response = await client.get(f"{ESI}/corporations/{corporation_id}/")
                corp_response.raise_for_status()
                corporation = corp_response.json()
                alliance_id = corporation.get("alliance_id")
                if alliance_id:
                    alliance_response = await client.get(f"{ESI}/alliances/{alliance_id}/")
                    alliance_response.raise_for_status()
                    alliance = alliance_response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Die öffentlichen Charakterdaten sind momentan nicht erreichbar.") from exc

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_name": APP_NAME,
            "character": character,
            "character_id": character_id,
            "corporation": corporation,
            "alliance": alliance,
        },
    )


@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/api/kills")
async def kills():
    return await get_kills()


@app.get("/health")
def health():
    return {"status": "ok"}
