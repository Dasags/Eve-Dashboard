# EVE-World

Modulares Eve-Online-Dashboard in Python (FastAPI) als Docker-Image.

## Start
```bash
# in docker-compose.yml ADMIN_PASSWORD, CONTACT, PUBLIC_URL setzen
docker compose up -d
```
Startseite: http://localhost:8000 · Admin (nicht verlinkt): http://localhost:8000/admin

## Admin-Login (`/admin`)
- **Passwort:** Variable `ADMIN_PASSWORD` in der Compose.
- **Authentik (OAuth2/OIDC):** `PUBLIC_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` setzen; Redirect-URI `PUBLIC_URL/admin/oidc/callback`. Optional `OIDC_ADMIN_GROUP` beschränkt auf eine Gruppe.
- Beides gleichzeitig ist möglich; ohne eines von beiden ist das Panel gesperrt.

## EVE-Login & Modul „Skills"
- App auf https://developers.eveonline.com/applications anlegen (Auth Flow: Authentication & API Access), Callback `PUBLIC_URL/auth/callback`, Scopes `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1`, `esi-wallet.read_character_wallet.v1`, `esi-assets.read_assets.v1`, `esi-markets.structure_markets.v1` und `esi-universe.read_structures.v1` (die beiden letzten nur für Structure-Märkte im Marketwatcher). Nach neuen Scopes müssen sich Charaktere einmal neu anmelden.
- `EVE_CLIENT_ID` und `EVE_CLIENT_SECRET` in die Compose eintragen.
- Module mit `login_required=True` erscheinen erst nach dem Login. Tokens liegen verschlüsselt in `/data/tokens.json` und bleiben bis zum Entfernen des Charakters (bzw. Konto-Löschen) gespeichert.
- **Main + Alts:** Wer sich zuerst anmeldet, wird Hauptcharakter seines Kontos. Unter `/auth/chars` fügst du Alts hinzu (eigener EVE-Login je Alt). Jedes Modul zeigt oben eine Charakter-Auswahl; der gewählte Charakter gilt für alle Module. Konten liegen in `/data/accounts.json`.
- Das Modul liest nur (Skill-Queue, Skills) – ESI bietet keinen Schreibzugriff auf die Warteschlange.

## Modul „Wallet" (standardmäßig aus)
Im Admin-Panel unter „Module" auf *Aktiv* schalten; optional den Zugriff auf bestimmte Hauptcharaktere beschränken. Zwei Seiten: **Wallet** (Kontostand, 30-Tage-Einkommen/-Ausgaben, Journal, Marktgeschäfte) und **Wallet-Verteilung** (Start-Kontostand, Beschriftungen mit %, Eingänge, Ausgaben). Beides je Charakter; Pläne in `/data/wallet_plans.json`. Charaktere, die sich vor dem Wallet-Modul angemeldet haben, müssen sich einmal neu anmelden (neuer Scope).

## Modul „Char-Dashboard"
Nach dem Login: Hauptcharakter, Corp (Dabei-seit, CEO, Logo, Allianz) und alle Charaktere mit laufendem Skill samt Restzeit und Security Status. Die **MOTD** oben stellst du im Admin-Panel ein. Nutzt öffentliche ESI-Daten plus den Skillqueue-Scope.

## Module freischalten
Nur das **Char-Dashboard** ist nach dem Login sofort verfügbar. Alle anderen Module (Skills, Wallet, …) sind standardmäßig aus und werden im Admin-Panel unter „Module" aktiviert. Neue Module sind per `default_enabled=False` in `Module(...)` ebenfalls zunächst gesperrt.

## Modul „Industry" (standardmäßig aus)
Produkt (Schiff, Modul, …) suchen → Explosionszeichnung mit Materialien, Jita-Preisen (4-4, niedrigster Verkaufspreis), Lagerbestand (Assets) und Dauer je Produktionsschritt. Komponenten und Reaktionen lassen sich einzeln „bauen" oder „kaufen". Rezeptdaten kommen einmalig aus der SDE (Fuzzwork), Preise von Fuzzwork-Market (Fallback ESI). Für den Lagerbestand wird der Scope `esi-assets.read_assets.v1` benötigt (einmal neu anmelden).

## Seite „Incursion" (standardmäßig aus)
Zeigt alle aktuellen Incursions mit Konstellation, Region, Staging-System, Fraktion, Status (Mobilisierung / Etabliert / Rückzug), Einfluss, Boss und befallenen Systemen. Nutzt nur öffentliche ESI-Daten (keine zusätzliche Berechtigung), aktualisiert sich alle 2 Minuten. Soll die Seite auch ohne Login sichtbar sein: in `app/modules/incursion/__init__.py` `login_required=False` setzen.

## Seite „Marketwatcher" (standardmäßig aus)
Listen mit Items (Suche oder EFT-Fitting importieren), regelmäßige Preisabfrage (Standard alle 15 Min., `MARKET_POLL_MINUTES`) aus Jita 4-4 und aus weiteren Standorten. **Weitere Standorte (z. B. Heimat-Staging) legst du einmal im Admin-Panel unter „Marketwatcher – Standorte" fest** (NPC-Station per ID oder Name, Structure per ID plus einem Charakter mit den Structure-Scopes und Dock-Zugriff). Im Marketwatcher wählt jeder Nutzer je Liste per Häkchen, welche Standorte abgefragt werden. Preise und Verlauf (30 Tage) liegen in `/data/marketwatch.db`, Listen je Konto in `/data/watchlists.json`, die Standorte in `/data/settings.json`.

## Modul „Planetary Industry" (standardmäßig aus)
PI-Produkt (P1–P4) suchen, Menge eingeben → komplette Produktionskette bis zu den Rohstoffen (P0) mit Bedarf, Zyklen, Dauer je Schritt und Preisen **in jedem Schritt** in Jita und in den vom Admin festgelegten Heimat-Standorten (je Standort per Häkchen wählbar). Mit „Einkaufsstufe" oder dem Knopf je Zeile legst du fest, was du kaufst statt selbst herzustellen. Schemata kommen einmalig aus der SDE (Fuzzwork), Preise 10 Minuten gecacht.

## Startseite
Die Startseite ist derzeit eine reine Informationsseite und zeigt den Projektstatus sowie geplante Dashboard-Bereiche. Sie fragt keine Live-Spieldaten ab. Die Dashboard-Funktionen werden schrittweise ergänzt.

## Struktur
- `app/main.py` – App-Factory
- `app/services/` – Kartendaten, Killfeed (Hintergrund-Tasks)
- `app/routers/` – Startseite, API, Auth-Platzhalter, Admin
- `app/modules/<name>/` – weitere Module (`module = Module(...)`), automatisch geladen
- `/data` (Volume) – Einstellungen, Session-Key, Karten-Cache
