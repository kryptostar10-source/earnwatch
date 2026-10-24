# EarnWatch – Passive Einnahmen sicher tracken

Ein schlankes Dashboard für deinen Proxmox-Docker-Host mit **eigener IP im Heimnetz**,
**HTTPS** und **Login**. Es ruft regelmäßig deine Kontostände ab bei

- **Honeygain** (USD) und **Honeygain JumpTask** getrennt – auf Wunsch mit zwei verschiedenen Honeygain-Konten
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

**Update (am besten einmal im Monat – holt auch Sicherheits-Updates der Images):**

```bash
sudo bash /opt/earnwatch/update.sh
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

## Benachrichtigungen aufs Handy

Unter *Einstellungen → Benachrichtigungen* einrichten – mit der kostenlosen App **ntfy** (ohne Anmeldung)
und/oder einem **Telegram**-Bot. Gemeldet werden (einzeln abschaltbar):

- 💰 Auszahlung möglich (Grenze erreicht)
- ⚠️ Abruf fehlgeschlagen / Login abgelaufen – und ✅ wenn es wieder läuft
- ⛏️ Duino-Miner offline / wieder online
- 🔐 neue Anmeldung bei EarnWatch, 🚨 gesperrte Login-Versuche
- 🔑 Passwort geändert, 🛡️ 2FA ein/aus
- 📊 Tageszusammenfassung zur Wunsch-Uhrzeit

Das ntfy-Thema ist wie ein Passwort: Wer es kennt, kann mitlesen. EarnWatch schlägt ein zufälliges vor.

## Honeygain-Verlauf

Beim ersten Abruf übernimmt EarnWatch die Tageseinnahmen der letzten 30 Tage direkt von Honeygain
(normal und JumpTask). Diagramm und Wochen-/Monatswerte sind dadurch sofort gefüllt.

## Backups & automatische Updates

`install.sh` richtet auf Wunsch ein:
- **jede Nacht um 3:30** ein Backup nach `/var/backups/earnwatch` (die letzten 7 bleiben, nur root lesbar)
- **jeden Sonntag** ein automatisches Update inkl. Sicherheits-Updates aller Images (vorher wird gesichert)

```bash
sudo bash /opt/earnwatch/backup.sh                 # Backup jetzt
sudo bash /opt/earnwatch/restore.sh                # zeigt vorhandene Backups
sudo bash /opt/earnwatch/restore.sh <datei>        # wiederherstellen
systemctl list-timers 'earnwatch-*'                # wann läuft was
```

Tipp: Kopiere ab und zu ein Backup auf einen anderen Rechner/USB-Stick. Es enthält den Schlüssel
zu deinen gespeicherten Zugangsdaten – also sicher aufbewahren.

## Als App aufs iPhone

EarnWatch lässt sich wie eine App auf den Home-Bildschirm legen (eigenes Icon, Vollbild, Tab-Leiste unten):
Seite in **Safari** öffnen → Teilen-Knopf → **„Zum Home-Bildschirm“**. Beim Login „30 Tage angemeldet bleiben“
anhaken, dann musst du dich nicht ständig neu einloggen. Unterwegs zusammen mit Tailscale (siehe unten) nutzen.
Tipp: Am besten über die Tailscale-Adresse hinzufügen – die hat ein echtes Zertifikat.

## Von unterwegs: nur EarnWatch über Tailscale

EarnWatch kann einen eigenen kleinen Tailscale-Container mitstarten. Dann ist **ausschließlich diese Seite**
in deinem Tailscale-Netz erreichbar – unter `https://earnwatch.<dein-tailnet>.ts.net` mit echtem Zertifikat.
Dein restliches Heimnetz bleibt unsichtbar, im Router muss nichts freigegeben werden, und aus dem Internet
ist die Seite weiterhin nicht erreichbar (Tailscale „Funnel“ ist ausdrücklich aus).

1. Kostenloses Konto auf https://login.tailscale.com anlegen
2. Unter https://login.tailscale.com/admin/dns **MagicDNS** und **HTTPS Certificates** einschalten
3. Unter https://login.tailscale.com/admin/settings/keys einen **Auth-Key** erzeugen (nicht wiederverwendbar)
4. `sudo bash install.sh` ausführen, bei „Fernzugriff über Tailscale“ `j` wählen und den Key einfügen
5. Tailscale-App aufs Handy, mit demselben Konto anmelden – fertig. Die Adresse zeigt `install.sh` am Ende an.

Tipps: In der Tailscale-Verwaltung beim Gerät „earnwatch“ **Key expiry deaktivieren**, sonst musst du es
nach 180 Tagen neu anmelden. Nur deine eigenen Geräte ins Tailnet aufnehmen. Und 2FA in EarnWatch einschalten.

**LAN-Sperre für die Earn-Apps:** Honeygain, EarnApp, Pawns und Earn.fm leiten fremden Internetverkehr
über deinen Anschluss. `install.sh` richtet auf Wunsch eine Firewall-Regel ein (`lan-guard.sh`, als
systemd-Dienst `earnwatch-lan-guard`), die diesen Containern den Zugriff auf dein Heimnetz verbietet –
Internet geht weiter. Status prüfen: `sudo /opt/earnwatch/lan-guard.sh status`

**Zertifikatswarnung dauerhaft loswerden (empfohlen):** Statt die Warnung jedes Mal wegzuklicken,
installiere das Stammzertifikat von Caddy einmal auf deinen Geräten:

```bash
sudo docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > earnwatch-root.crt
```

Die Datei auf PC/Mac/Handy kopieren und als vertrauenswürdige Zertifizierungsstelle importieren
(Windows: Doppelklick → „Vertrauenswürdige Stammzertifizierungsstellen“, Mac: Schlüsselbundverwaltung →
„Immer vertrauen“). Danach zeigt der Browser das Schloss – und eine echte Warnung würde auffallen.

**Hinweis Sicherheit:** Die Zugangsdaten von Grass, EarnApp und Bytelixir sind Login-Tokens.
Wer sie hat, kommt in dein Konto. EarnWatch speichert sie verschlüsselt und zeigt sie nie wieder an.

**Backup:** Das Volume `earnwatch_earnwatch_data` enthält die Datenbank **und** den
Verschlüsselungsschlüssel (`.fernet.key`). Sichere immer beides zusammen.
Ohne Schlüssel sind die gespeicherten Zugangsdaten nicht mehr lesbar; du müsstest sie neu eingeben.

```bash
docker run --rm -v earnwatch_earnwatch_data:/d -v "$PWD":/b alpine tar czf /b/earnwatch-backup.tgz -C /d .
```

**Zwei-Faktor-Anmeldung (2FA):** Unter *Einstellungen → Zwei-Faktor-Anmeldung → Mit QR-Code einrichten*
den QR-Code mit einer Authenticator-App scannen (Google/Microsoft Authenticator, Aegis, 2FAS, Bitwarden …).
Danach wird beim Login zusätzlich ein 6-stelliger Code abgefragt. Die 8 Wiederherstellungscodes sicher aufbewahren –
jeder funktioniert einmal, falls das Handy weg ist.

**Handy UND Wiederherstellungscodes verloren?** 2FA direkt auf dem Server abschalten:

```bash
cd /opt/earnwatch && sudo docker compose stop earnwatch
sudo docker run --rm -v earnwatch_earnwatch_data:/d alpine sh -c "apk add -q sqlite && sqlite3 /d/earnwatch.db 'UPDATE users SET totp_secret=NULL, recovery=NULL, session_ver=session_ver+1'"
sudo docker compose start earnwatch
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

## Schriften

Die Oberfläche nutzt **Sora** und **JetBrains Mono** (beide SIL Open Font License, Lizenzen in
`app/earnwatch/static/fonts/`). Sie werden lokal ausgeliefert – es wird nichts von Google nachgeladen.
