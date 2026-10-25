#!/usr/bin/env bash
# Zugangsdaten der Earn-Apps (Honeygain, Pawns, Earn.fm) neu eintragen und den Container neu starten.
# Aufruf:  sudo bash zugangsdaten.sh            (fragt, welche App)
#          sudo bash zugangsdaten.sh pawns
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen: sudo bash zugangsdaten.sh"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
[ -f .env ] || { echo ".env fehlt – erst install.sh ausführen"; exit 1; }
umask 077
setenv(){ # wie in install.sh: Sonderzeichen bleiben erhalten
  grep -v -E "^$1=" .env > .env.tmp || true
  if [[ "$2" != *"'"* ]]; then printf "%s='%s'\n" "$1" "$2" >> .env.tmp
  else v=${2//\\/\\\\}; v=${v//\"/\\\"}; v=${v//\$/\$\$}; printf '%s="%s"\n' "$1" "$v" >> .env.tmp; fi
  mv .env.tmp .env; chmod 600 .env
}
APP="${1:-}"
if [ -z "$APP" ]; then
  echo "Welche App?  1) Honeygain   2) Pawns   3) Earn.fm"
  read -rp "Nummer: " n
  case "$n" in 1) APP=honeygain;; 2) APP=pawns;; 3) APP=earnfm;; *) echo "Abbruch"; exit 1;; esac
fi
case "$APP" in
  honeygain)
    echo "Tipp: Hast du dich bei Honeygain mit Google/Apple angemeldet? Dann zuerst auf"
    echo "      dashboard.honeygain.com -> 'Passwort vergessen' ein eigenes Passwort setzen."
    read -rp "Honeygain E-Mail: " e; read -rsp "Honeygain Passwort: " p; echo
    [ -n "$e" ] && [ -n "$p" ] || { echo "Leer – Abbruch"; exit 1; }
    setenv HG_EMAIL "$e"; setenv HG_PASSWORD "$p" ;;
  pawns)
    echo "Tipp: Bei Google-Login auf dashboard.pawns.app -> Einstellungen ein eigenes Passwort setzen."
    read -rp "Pawns E-Mail: " e; read -rsp "Pawns Passwort: " p; echo
    [ -n "$e" ] && [ -n "$p" ] || { echo "Leer – Abbruch"; exit 1; }
    setenv PAWNS_EMAIL "$e"; setenv PAWNS_PASSWORD "$p" ;;
  earnfm)
    read -rp "Earn.fm API-Token (app.earn.fm -> Einstellungen): " t
    [ -n "$t" ] || { echo "Leer – Abbruch"; exit 1; }
    setenv EARNFM_TOKEN "$t" ;;
  *) echo "Unbekannte App: $APP"; exit 1 ;;
esac
echo "✓ Gespeichert in $DIR/.env"

docker compose stop "$APP" >/dev/null 2>&1 || true
read -rp "Hat die App dich wegen zu vieler Login-Versuche gesperrt ('too many tries')? [j/N] " lock
if [[ "${lock,,}" == j* ]]; then
  # Container-Wächter 70 Minuten lang für diese App pausieren, dann automatisch starten
  mkdir -p /var/lib/earnwatch-health && chmod 700 /var/lib/earnwatch-health
  echo $(( $(date +%s) + 70*60 )) > "/var/lib/earnwatch-health/$APP.pause"
  if systemd-run --quiet --unit="earnwatch-start-$APP-$(date +%s)" --on-active=60m \
       /usr/bin/env bash -c "cd '$DIR' && docker compose up -d --force-recreate '$APP'"; then
    echo "✓ $APP startet in 60 Minuten automatisch mit den neuen Daten (Sperre läuft dann ab)."
  else
    echo "Bitte in einer Stunde selbst starten:  cd $DIR && sudo docker compose up -d --force-recreate $APP"
  fi
else
  docker compose up -d --force-recreate "$APP"
  echo "Warte 20 Sekunden und zeige das Log …"; sleep 20
  docker logs --tail 8 "$APP" 2>&1 | sed 's/^/  /'
  st=$(docker inspect -f '{{.State.Status}}' "$APP" 2>/dev/null || echo "?")
  echo "Zustand: $st"
  [ "$st" = "running" ] && echo "✓ Läuft." || echo "Läuft noch nicht – E-Mail/Passwort nochmal prüfen."
fi
