import os
import time
from urllib.parse import urlencode, urlsplit

import httpx

from app.services import storage

SSO = "https://login.eveonline.com"
ESI_BASE = "https://esi.evetech.net/latest"
SCOPE_NAMES = [
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-assets.read_assets.v1",
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
]


def public_url() -> str:
    value = os.getenv("PUBLIC_URL", "").rstrip("/")
    parsed = urlsplit(value)
    if not (
        (parsed.scheme == "https" and parsed.hostname)
        or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})
    ):
        raise RuntimeError("PUBLIC_URL muss auf die öffentliche Dashboard-Adresse zeigen.")
    if parsed.path or parsed.query or parsed.fragment:
        raise RuntimeError("PUBLIC_URL darf keinen Pfad, Query-String oder Fragment enthalten.")
    return value


def oauth_credentials() -> tuple[str, str]:
    client_id = os.getenv("EVE_CLIENT_ID")
    client_secret = os.getenv("EVE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("EVE_CLIENT_ID und EVE_CLIENT_SECRET sind nicht konfiguriert.")
    return client_id, client_secret


def authorization_url(state: str, scopes: list[str] | None = None) -> str:
    client_id, _ = oauth_credentials()
    query = urlencode(
        {
            "response_type": "code",
            "redirect_uri": f"{public_url()}/auth/callback",
            "client_id": client_id,
            "scope": " ".join(scopes if scopes is not None else SCOPE_NAMES),
            "state": state,
        }
    )
    return f"{SSO}/v2/oauth/authorize?{query}"


def token_scopes(token: dict, identity: dict) -> list[str]:
    scopes = token.get("scope") or identity.get("Scopes") or identity.get("scopes") or []
    if isinstance(scopes, str):
        return scopes.split()
    if isinstance(scopes, list):
        return [scope for scope in scopes if isinstance(scope, str)]
    return []


async def exchange_code(code: str) -> tuple[dict, dict]:
    client_id, client_secret = oauth_credentials()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SSO}/v2/oauth/token",
            auth=(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{public_url()}/auth/callback",
            },
        )
        response.raise_for_status()
        token = response.json()
        identity_response = await client.get(
            f"{SSO}/oauth/verify",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        identity_response.raise_for_status()
        identity = identity_response.json()
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 0))
    token["scope"] = token_scopes(token, identity)
    return identity, token


async def refresh_token(character_id: int) -> dict:
    character = storage.get_character(character_id)
    if not character:
        raise LookupError("Charakter nicht gefunden.")
    token = character["token"]
    if token.get("expires_at", 0) > time.time() + 60:
        return token
    refresh = token.get("refresh_token")
    if not refresh:
        raise PermissionError("Für diesen Charakter ist kein Refresh-Token verfügbar.")

    client_id, client_secret = oauth_credentials()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{SSO}/v2/oauth/token",
            auth=(client_id, client_secret),
            data={"grant_type": "refresh_token", "refresh_token": refresh},
        )
        response.raise_for_status()
        renewed = response.json()
    renewed["refresh_token"] = renewed.get("refresh_token", refresh)
    scope = renewed.get("scope") or " ".join(character["scopes"])
    renewed["scope"] = scope.split() if isinstance(scope, str) else scope
    renewed["expires_at"] = int(time.time()) + int(renewed.get("expires_in", 0))
    storage.save_character(
        character_id,
        character["name"],
        renewed,
        character["account_id"],
    )
    return renewed


class ESIClient:
    def __init__(self, character_id: int | None = None):
        self.character_id = character_id
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            timeout=25,
            headers={
                "User-Agent": f"{os.getenv('APP_NAME', 'EVE-World')} ({os.getenv('CONTACT', 'configure CONTACT')})",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self.client:
            await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.client:
            raise RuntimeError("ESI-Client muss in einem async with-Block verwendet werden.")
        headers = dict(kwargs.pop("headers", {}))
        if self.character_id is not None:
            token = await refresh_token(self.character_id)
            headers["Authorization"] = f"Bearer {token['access_token']}"
        response = await self.client.request(
            method,
            path if path.startswith("http") else f"{ESI_BASE}{path}",
            headers=headers,
            **kwargs,
        )
        if response.status_code == 401 and self.character_id is not None:
            character = storage.get_character(self.character_id)
            if not character:
                response.raise_for_status()
            token = character["token"]
            token["expires_at"] = 0
            storage.save_character(
                self.character_id,
                character["name"],
                token,
                character["account_id"],
            )
            renewed = await refresh_token(self.character_id)
            headers["Authorization"] = f"Bearer {renewed['access_token']}"
            response = await self.client.request(
                method,
                path if path.startswith("http") else f"{ESI_BASE}{path}",
                headers=headers,
                **kwargs,
            )
        response.raise_for_status()
        return response

    async def get(self, path: str, **kwargs) -> dict | list:
        return (await self.request("GET", path, **kwargs)).json()

    async def post(self, path: str, **kwargs) -> dict | list:
        return (await self.request("POST", path, **kwargs)).json()
