# EVE-World

Ein webbasiertes EVE-Online-Dashboard mit EVE-SSO-Anmeldung, Charakteren und Produktions-/Marktwerkzeugen. Spielwerte werden live aus ESI, zKillboard und dem Fuzzwork-SDE geladen; Vorschauwerte aus der PDF werden nicht als echte Spieldaten verwendet.

## Funktionen

- **Startseite:** Sternenkarte, konfigurierbarer Killfeed und Projektinformation.
- **Charaktere:** EVE-SSO, Main-/Alt-Verwaltung, Charakter-, Corporation- und Allianzprofil.
- **Skills:** trainierte Skills, Skillpunkte und Skill-Warteschlange.
- **Wallet:** Kontostand, Journal, Transaktionen und manuelle Verteilungsplanung.
- **Marketwatcher:** mehrere Itemlisten, EFT-Fitting-Import, Preise an NPC-Stationen oder Structures und Preisverlauf.
- **Industry:** Materialbedarf, optionale Komponentenfertigung, Charakter-Assetbestand, Produktionszeiten und Kostenschätzung.
- **Planetary Industry:** PI-Ketten mit wählbarer Einkaufsstufe, Zykluszeiten und Preisvergleich je Standort.
- **Incursions:** aktuelle Incursion-Daten aus ESI.
- **Administration:** Passwort- oder optionaler Authentik-OIDC-Login, Module, Standorte und Startseiten-Einstellungen.

Die Module (außer Dashboard) sind zunächst deaktiviert und können unter `/admin` eingeschaltet werden. Industrie-Berechnungen berücksichtigen die eingegebenen Boni, die Zeit-Skills des Charakters (Industry, Advanced Industry, Reactions; wahlweise alle Level 5 oder keine) und optional die T2-Erfindung (Wahrscheinlichkeit aus Skills, Datacores, Decryptor, Auftragskosten, erwartete Versuche). Nicht eingerechnet: Blueprint-Kopierzeit/-kosten. Decryptor-Werte und Erfindungs-Auftragskosten sind Näherungen und sollten gegen das Spiel geprüft werden. Preise und externe Daten hängen von der Erreichbarkeit und den Berechtigungen der jeweiligen ESI-Endpunkte ab. ESI-/SDE-Daten konnten nicht in jeder Umgebung live geprüft werden.

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

Das Docker-Volume `eve-dashboard-data` enthält Datenbank, verschlüsselte Tokens, Preisverläufe und externe Daten-Caches. Es bleibt bei einem Container-Neustart erhalten und sollte regelmäßig gesichert werden. Der Container benötigt ausgehenden Internetzugriff auf EVE SSO, ESI, zKillboard und Fuzzwork.
