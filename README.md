# EVE-World

Ein schrittweise wachsendes Dashboard für EVE Online. Der aktuelle Stand ist ein Grundgerüst mit Informationsseite und einem ersten Charakter-Überblick nach Anmeldung mit EVE SSO.

## Aktueller Funktionsumfang

- `/` – Informationsseite mit Projektstatus.
- `/auth/login` – Anmeldung über EVE SSO.
- `/dashboard` – öffentlicher Charakter, Corporation, Allianz und Sicherheitsstatus des angemeldeten Charakters.
- `/auth/logout` – Sitzung beenden.
- `/api/kills` – Killfeed-Daten als JSON; wird auf der Informationsseite nicht eingeblendet.
- `/health` – Docker-Healthcheck.

Für den Charakter-Überblick werden nur öffentliche ESI-Daten geladen. Das EVE-Zugangstoken wird nicht dauerhaft gespeichert. Admin-Panel, Skills, Skillqueue, Alts, Karte und weitere Spielmodule sind noch nicht implementiert.

## EVE SSO konfigurieren

1. Eine EVE-Anwendung auf https://developers.eveonline.com/applications anlegen.
2. Als Callback-URL exakt `PUBLIC_URL/auth/callback` eintragen. Für diesen ersten Überblick sind keine ESI-Scopes erforderlich.
3. In `docker-compose.yml` die Werte `PUBLIC_URL`, `EVE_CLIENT_ID` und `EVE_CLIENT_SECRET` setzen.
4. `SESSION_SECRET` durch einen geheimen Zufallswert ersetzen, z. B. erzeugt mit `openssl rand -hex 32`. Diesen Wert nicht veröffentlichen oder committen.
5. `COOKIE_SECURE` bei HTTPS auf `true` lassen. Für einen lokalen HTTP-Test auf `false` setzen und `PUBLIC_URL` auf `http://localhost:8000` stellen.

`CONTACT` sollte eine gültige Kontaktangabe für die ESI-Anfragen enthalten.

## Starten

Die Compose-Datei enthält aktuell noch einen Platzhalter für das Container-Image. Für einen lokalen Build im Service `eve-world` `build: .` aktivieren und die `image:`-Zeile anpassen oder entfernen. Danach:

```bash
docker compose up --build -d
```

Das Verzeichnis `./data` bleibt als Volume erhalten. In Dockhand gelten dieselben Compose- und Umgebungsvariablen.
