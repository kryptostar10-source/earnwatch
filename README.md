# EarnWatch – Passive Einnahmen sicher tracken

Ein schlankes Dashboard für deinen Proxmox-Docker-Host mit **eigener IP im Heimnetz**,
**HTTPS** und **Login**. Es ruft regelmäßig deine Kontostände ab bei

- **Honeygain** (USD), auch im **JumpTask-Modus** – dann erscheint automatisch eine zweite Karte „Honeygain (JumpTask)“
- **EarnApp** (USD)
- **Grass** (Punkte – haben keinen festen Dollarwert und werden separat gezeigt)
- **Bytelixir** (USD)
- **Pawns.app** (IPRoyal, USD)
- **Earn.fm** (USD)
- **Wipter** (USD, Kontostand wird manuell eingetragen – Wipter hat keine Schnittstelle)
- **Duino-Coin** (DUCO, mit aktuellem Kurs in USD umgerechnet; zeigt auch deine aktiven Miner, z. B. ESP32)

und zeigt daraus Einnahmen **heute, diese Woche, diesen Monat, dieses Jahr** und gesamt,
ein Diagramm nach **Tagen, Wochen, Monaten oder Jahren**, den Guthaben-Verlauf,
erkannte Auszahlungen und eine Monats-Prognose.
Optional startet es die Honeygain- und EarnApp-Container gleich mit (wie CashPilot).

## Schnellstart (Terminal)

Auf deinem Docker-Server (VM auf Proxmox) als root:

```bash
git clone https://github.com/kryptostar10-source/earnwatch.git /opt/earnwatch
cd /opt/earnwatch
bash install.sh
```

Das Skript erkennt Netzwerkkarte, Netz und Router, fragt nach einer freien IP für EarnWatch,
erzeugt das Einrichtungs-Token und startet alles. Danach vom PC/Handy `https://<deine-IP>` öffnen.

**Update:**

```bash
cd /opt/earnwatch && sudo git pull && sudo docker compose up -d --build
```

**Earn-Apps als Container nachträglich einschalten:** einfach `sudo bash install.sh` nochmal ausführen
(deine Einstellungen bleiben erhalten) oder in der `.env` z. B. `COMPOSE_PROFILES=honeygain,earnapp,pawns,earnfm` setzen.

## Was ist anders/sicherer als CashPilot?

| | CashPilot | EarnWatch |
|---|---|---|
| Zugriff auf `/var/run/docker.sock` | ja (Worker kann jeden Container starten = Root auf dem Host) | **nein** – Earn-Apps stehen fest in der `docker-compose.yml` |
| Erreichbarkeit | Port am Host | **eigene LAN-IP**, nur **HTTPS**, nur aus privaten Netzen |
| Login | ja | ja: scrypt-Passwort-Hash, Sperre nach 5 Fehlversuchen, 12 h Sitzung |
| Zugangsdaten | verschlüsselt | verschlüsselt (Fernet/AES), werden **nie** wieder angezeigt oder geloggt |
| Web-Schutz | – | CSRF-Token + Origin-Prüfung, strikte CSP (kein JavaScript), `SameSite=Strict`-Cookies |
| Container | – | App läuft als Nicht-Root, Dateisystem read-only, alle Linux-Capabilities entfernt |
| Ersteinrichtung | – | erstes Konto nur mit geheimem Einrichtungs-Token aus der `.env` |

## Installation auf Proxmox

Voraussetzung: eine VM (empfohlen) oder ein LXC mit Docker + Docker Compose.

```bash
# 1. Ordner auf den Docker-Host kopieren (z. B. per scp) und hinein wechseln
cd earnwatch

# 2. Konfiguration anlegen
cp .env.example .env
nano .env
```

In der `.env` anpassen:

- `EW_IP` – eine **freie** IP in deinem Heimnetz, die **außerhalb des DHCP-Bereichs** deines Routers liegt (z. B. `192.168.1.50`)
- `EW_SUBNET` / `EW_GATEWAY` – dein Heimnetz (Fritzbox meist `192.168.178.0/24` und `192.168.178.1`)
- `EW_PARENT_IF` – Netzwerkkarte der VM, mit `ip a` nachsehen (oft `eth0` oder `ens18`)
- `EW_SETUP_TOKEN` – ein zufälliger Wert, erzeugen mit `openssl rand -base64 24`

```bash
# 3. Starten (nur Dashboard)
docker compose up -d --build

#    …oder Dashboard + Honeygain- und EarnApp-Container
docker compose --profile earners up -d --build
```

4. Im Browser `https://<EW_IP>` öffnen. Die Zertifikatswarnung einmal bestätigen
   (das Zertifikat ist selbst erzeugt, das ist im Heimnetz normal).
5. Einrichtungs-Token eingeben, Benutzername und Passwort festlegen.
6. Unter **Einstellungen** die Zugangsdaten eintragen (nur die Plattformen, die du nutzt):
   - **Honeygain:** E-Mail und Passwort
   - **EarnApp:** auf earnapp.com einloggen → `F12` → *Anwendung/Speicher* → *Cookies* → Wert von `oauth-refresh-token` kopieren
   - **Grass:** app.grass.io einloggen → `F12` → *Anwendung* → *Lokaler Speicher* → `https://app.grass.io` → Wert von `accessToken` (bzw. `token`) kopieren
   - **Bytelixir:** dash.bytelixir.com einloggen **mit „Remember Me“** → `F12` → *Anwendung* → *Cookies* → `bytelixir_session`, `remember_web_…` und `XSRF-TOKEN` kopieren. Wichtig ist `remember_web_…`, das hält etwa ein Jahr.
   - **Pawns.app / Earn.fm:** E-Mail und Passwort wie auf der Webseite
   - **Wipter:** keinen Zugang nötig – den Kontostand trägst du direkt auf der Übersicht ein
   - **Duino-Coin:** nur dein Benutzername (Duino-Kontostände sind öffentlich, kein Passwort nötig)
7. Auf **Jetzt abrufen** klicken. Danach läuft der Abruf automatisch (Standard: stündlich).

> Tageseinnahmen entstehen aus der Differenz zweier Abrufe. Das Diagramm füllt sich deshalb erst nach ein paar Stunden.
> Sinkt ein Guthaben, wird das als Auszahlung verbucht.

## Wichtige Hinweise

- **Der Proxmox-/Docker-Host selbst kann die macvlan-IP nicht erreichen.** Das ist eine Eigenheit von macvlan.
  Alle anderen Geräte im Heimnetz (PC, Handy, MacBook) können es. Zum Testen also vom PC aus öffnen.
- In Proxmox bei der VM unter *Hardware → Netzwerkkarte* die **Firewall bzw. den MAC-Filter** ausschalten,
  falls die IP nicht erreichbar ist (macvlan nutzt eine zusätzliche MAC-Adresse).
- **LXC statt VM:** Der Container braucht `nesting=1`, und macvlan klappt in unprivilegierten LXC oft nicht. Eine VM ist einfacher.
- **Honeygain** erlaubt nur **1 Gerät pro öffentlicher IP**. Läuft Honeygain schon auf einem anderen Gerät bei dir, verdient der Container nichts.
- **EarnApp-Container:** Nach dem ersten Start musst du das Gerät einmal verknüpfen. Öffne dazu
  `https://earnapp.com/r/<deine EARNAPP_UUID>` im Browser, während du eingeloggt bist.
  EarnApp sieht Container-Betrieb offiziell nicht gern; das Image `fazalfarhan01/earnapp` ist ein Community-Image.
- Die Earn-Apps laufen in einem eigenen Docker-Netz, getrennt vom Dashboard.
- **Grass und Bytelixir** gibt es nicht als offiziellen Docker-Container. Sie laufen weiter als App bzw.
  Browser-Erweiterung auf deinen Geräten; EarnWatch liest nur den Kontostand mit.
- **Duino-Coin** schürfst du wie gewohnt (z. B. mit deinen ESP32); EarnWatch zeigt Guthaben, Miner-Anzahl und Hashrate.
- **Grass-Punkte** werden je Epoche neu gezählt. Sinkt der Stand, wertet EarnWatch das als neue Epoche, nicht als Auszahlung.
- **Bytelixir** hat keine offizielle Schnittstelle; der Kontostand wird aus der Dashboard-Seite gelesen.
  Ändert Bytelixir die Seite, kann der Abruf ausfallen.
- Honeygain mit Google-/Apple-Login: Setze im Honeygain-Konto ein Passwort, sonst klappt der Abruf nicht.
  Ist bei Honeygain 2FA oder ein Captcha aktiv, kann der automatische Login scheitern.

## Pflege

```bash
docker compose logs -f earnwatch          # Log ansehen (enthält nie Passwörter/Tokens)
docker compose pull && docker compose up -d --build   # aktualisieren
```

**Hinweis Sicherheit:** Die Zugangsdaten von Grass, EarnApp und Bytelixir sind Login-Tokens.
Wer sie hat, kommt in dein Konto. EarnWatch speichert sie verschlüsselt und zeigt sie nie wieder an.

**Backup:** Das Volume `earnwatch_earnwatch_data` enthält die Datenbank **und** den
Verschlüsselungsschlüssel (`.fernet.key`). Sichere immer beides zusammen.
Ohne Schlüssel sind die gespeicherten Zugangsdaten nicht mehr lesbar; du müsstest sie neu eingeben.

```bash
docker run --rm -v earnwatch_earnwatch_data:/d -v "$PWD":/b alpine tar czf /b/earnwatch-backup.tgz -C /d .
```

**Passwort vergessen?** Container stoppen, Datenbank-Tabelle `users` leeren, neu starten und mit dem
Einrichtungs-Token ein neues Konto anlegen:

```bash
docker compose stop earnwatch
docker run --rm -v earnwatch_earnwatch_data:/d alpine sh -c "apk add -q sqlite && sqlite3 /d/earnwatch.db 'DELETE FROM users'"
docker compose start earnwatch
```

## Aufbau

```
earnwatch/
├── docker-compose.yml   # App + Caddy (HTTPS, eigene IP) + optionale Earn-Apps
├── Caddyfile            # HTTPS mit lokalem Zertifikat, nur private Netze erlaubt
├── .env.example
└── app/
    ├── Dockerfile
    ├── requirements.txt
    └── earnwatch/       # Python-App (Starlette)
        ├── main.py        # Routen, Login, Sicherheits-Header, Zeitplan
        ├── auth.py        # scrypt, Login-Sperre, CSRF
        ├── collectors.py  # Abruf Honeygain / EarnApp
        ├── crypto.py      # Verschlüsselung der Zugangsdaten
        ├── db.py, stats.py, charts.py
        ├── templates/ static/
```

Weitere Anbieter lassen sich in `collectors.py` ergänzen (Eintrag in `PLATFORMS` + Abruf-Funktion).
