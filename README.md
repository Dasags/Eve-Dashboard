# EVE-World

Ein webbasiertes EVE-Online-Dashboard mit EVE-SSO-Anmeldung, Charakteren und Produktions-/Marktwerkzeugen. Spielwerte werden live aus ESI, zKillboard und dem Fuzzwork-SDE geladen; Vorschauwerte aus der PDF werden nicht als echte Spieldaten verwendet.

## Funktionen

- **Übersicht:** Vollbreite Space-Oberfläche mit EVE-/Lokalzeit, Pilotenkarten (Wallet, aktueller Skill, Corporation) und Killfeed.
- **Sternenkarte:** Eigene interaktive 2D-Karte mit Regionen-, Sicherheits-, Killmail- und Incursion-Filtern; Systemdaten und Sprungverbindungen kommen aus dem EVE-SDE/Fuzzwork-Cache. DOTLAN wird als externe Referenz verlinkt, dessen Kartengrafiken werden nicht kopiert.
- **Charaktere:** EVE-SSO, Main-/Alt-Verwaltung, Charakter-, Corporation- und Allianzprofil.
- **Skills:** trainierte Skills, Skillpunkte und Skill-Warteschlange.
- **Wallet:** Kontostand, Journal, Transaktionen und manuelle Verteilungsplanung.
- **Assets:** Gegenstände und Schiffe des ausgewählten Charakters mit Standortfilter, Namenssuche und Vergleich der niedrigsten Verkaufsorders in Jita und einem konfigurierten Heimatmarkt. Inhalte von Schiffen und Containern erscheinen am äußeren Standort; unbekannte Standorte zeigen ihre ESI-ID.
- **Marketwatcher:** mehrere Itemlisten, EFT-Fitting-Import, Preise an NPC-Stationen oder Structures und Preisverlauf.
- **Industry:** Materialbedarf, optionale Komponentenfertigung, Charakter-Assetbestand, Produktionszeiten und Kostenschätzung.
- **Planetary Industry:** PI-Ketten mit wählbarer Einkaufsstufe, Zykluszeiten und Preisvergleich je Standort.
- **Incursions:** aktuelle Incursion-Daten aus ESI.
- **Administration:** Passwort- oder optionaler Authentik-OIDC-Login, Module, Standorte und Startseiten-Einstellungen.

Die Module (außer Dashboard) sind zunächst deaktiviert und können unter `/admin` eingeschaltet werden. Zusätzlich lassen sich unter `/admin` → „Spieler-Freigaben“ einzelne Module für einzelne Spieler freigeben. Industrie-Berechnungen berücksichtigen die eingegebenen Boni, die Zeit-Skills des Charakters (Industry, Advanced Industry, Reactions; wahlweise alle Level 5 oder keine) und optional die T2-Erfindung (Wahrscheinlichkeit aus Skills, Datacores, Decryptor, Auftragskosten, erwartete Versuche). Nicht eingerechnet: Blueprint-Kopierzeit/-kosten. Decryptor-Werte und Erfindungs-Auftragskosten sind Näherungen und sollten gegen das Spiel geprüft werden. Preise und externe Daten hängen von der Erreichbarkeit und den Berechtigungen der jeweiligen ESI-Endpunkte ab. ESI-/SDE-Daten konnten nicht in jeder Umgebung live geprüft werden.

Für Assets benötigt jeder gewählte Charakter `esi-assets.read_assets.v1`. Heimatmärkte werden unter `/admin` → Standorte eingerichtet. Für Structure-Namen benötigt der Asset-Charakter `esi-universe.read_structures.v1` und Zugriff auf die Structure; für deren Markt benötigt der am Standort konfigurierte Charakter `esi-markets.structure_markets.v1`. Fehlende Namen oder Zugriffe werden mit der von ESI gelieferten Standort-ID angezeigt. Der Vergleich nutzt die niedrigste Verkaufsorder am jeweiligen Standort; fehlende Orders oder Fehler sind keine Nullpreise. Marktpreise werden nur für die 20 sichtbaren Positionen geladen.

## EVE-SSO einrichten

1. Eine EVE-Anwendung unter <https://developers.eveonline.com/applications> erstellen.
2. Als Callback exakt `PUBLIC_URL/auth/callback` eintragen.
3. Diese Scopes für die Anwendung aktivieren:
   - `esi-skills.read_skills.v1`
   - `esi-skills.read_skillqueue.v1`
   - `esi-wallet.read_character_wallet.v1`
   - `esi-assets.read_assets.v1`
   - `esi-markets.structure_markets.v1`
   - `esi-universe.read_structures.v1`
4. `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET` und `PUBLIC_URL` in Compose/Dockhand konfigurieren.
5. Für `SESSION_SECRET` einen festen geheimen Wert erzeugen, z. B. `openssl rand -hex 32`, und ihn in Dockhand oder einer nicht eingecheckten `.env`-Datei setzen. **Nicht ändern**, solange die Datenbank verwendete EVE-Tokens enthält: der Schlüssel verschlüsselt diese Tokens.
6. `ADMIN_PASSWORD` durch ein eigenes Passwort ersetzen. Alternativ oder zusätzlich Authentik-OIDC einrichten. `CONTACT` sollte eine gültige Kontaktangabe für ESI/zKillboard sein.
7. `COOKIE_SECURE` bei HTTPS auf `true` lassen. Für einen lokalen HTTP-Test `PUBLIC_URL=http://localhost:8000` und `COOKIE_SECURE=false` setzen.

Scopes, die beim ersten Login nicht freigegeben wurden, können durch erneute Anmeldung mit EVE SSO nicht ergänzt werden, solange eine aktive Sitzung besteht. Abmelden und erneut anmelden; für zusätzliche Alts den vorhandenen Charakter anmelden und „Alt hinzufügen“ verwenden.

## Start und Update

Die Compose-Datei baut das Image lokal aus dem Repository. Variablen und Passwörter vor dem produktiven Einsatz anpassen:

```bash
docker compose up --build -d
```

In Dockhand nach Änderungen am Repository den Stack neu bauen bzw. mit Image-Build neu deployen. Bei Verwendung eines veröffentlichten GHCR-Images stattdessen den `build:`-Eintrag entfernen und das Image unter `image:` eintragen; nach einem neuen Image-Build Image neu laden und den Stack aktualisieren.

Beim Start wird automatisch eine Sicherung der Datenbank in `/data/backups` angelegt (die letzten 7 bleiben). Wallet-Pläne sind pro Charakter gespeichert und liegen in der Datenbank. **Wichtig:** `SESSION_SECRET` muss bei jedem Neubau gleich bleiben, sonst können die gespeicherten EVE-Tokens nicht mehr entschlüsselt werden, und das Volume `eve-dashboard-data` darf nicht gelöscht werden (kein `down -v`).

**PostgreSQL:** Die Compose-Datei startet einen `db`-Container (PostgreSQL 16) und setzt `DATABASE_URL`. Beim ersten Start mit PostgreSQL wird eine vorhandene SQLite-Datenbank aus `/data/dashboard.sqlite3` automatisch einmalig übernommen (Konten, Tokens, Wallet, Marktwatcher, Einstellungen). Dafür das Volume `eve-dashboard-data` beibehalten und `POSTGRES_PASSWORD` setzen (Dockhand: Umgebungsvariable). Das Volume `eve-dashboard-pg` enthält die Daten und darf nicht gelöscht werden; Sicherung z. B. mit `docker exec eve-world-db pg_dump -U eveworld eveworld > backup.sql`. Die automatischen Sicherungen unter `/data/backups` gelten nur im SQLite-Modus. Ohne `DATABASE_URL` läuft die App weiterhin mit SQLite.

Das Docker-Volume `eve-dashboard-data` enthält Datenbank, verschlüsselte Tokens, Preisverläufe und externe Daten-Caches. Es bleibt bei einem Container-Neustart erhalten und sollte regelmäßig gesichert werden. Der Container benötigt ausgehenden Internetzugriff auf EVE SSO, ESI, zKillboard und Fuzzwork.
