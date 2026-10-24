#!/usr/bin/env bash
# EarnWatch Installer – fragt die Netzwerkdaten ab, schreibt die .env und startet EarnWatch.
# Aufruf im geklonten Ordner:  bash install.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say(){ printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die(){ printf '\n\033[1;31mFehler: %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen (sudo bash install.sh)"
command -v docker >/dev/null || die "Docker ist nicht installiert"
docker compose version >/dev/null 2>&1 || die "'docker compose' fehlt (Paket docker-compose-plugin installieren)"

cd "$DIR"

if [ -f .env ]; then
  say "Vorhandene .env gefunden – Einstellungen bleiben erhalten"
else
  IF=$(ip route show default | awk '{print $5; exit}')
  GW=$(ip route show default | awk '{print $3; exit}')
  NET=$(ip -4 route show dev "$IF" proto kernel scope link | awk '{print $1; exit}')
  MYIP=$(ip -4 -o addr show dev "$IF" | awk '{print $4; exit}' | cut -d/ -f1)
  say "Erkanntes Netzwerk"
  echo "  Netzwerkkarte: $IF"
  echo "  Server-IP:     $MYIP"
  echo "  Netz:          $NET"
  echo "  Router:        $GW"
  read -rp "Stimmt das? [J/n] " ok
  if [[ "${ok,,}" == n* ]]; then
    read -rp "Netzwerkkarte: " IF; read -rp "Netz (z. B. 192.168.178.0/24): " NET; read -rp "Router-IP: " GW
  fi
  while true; do
    read -rp "Freie IP für EarnWatch (außerhalb des DHCP-Bereichs, z. B. ${NET%.*}.250): " EWIP
    [[ "$EWIP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Ungültige IP"; continue; }
    if ping -c 2 -W 1 "$EWIP" >/dev/null 2>&1; then echo "Die IP $EWIP antwortet schon – bitte eine andere nehmen."; continue; fi
    break
  done
  TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' )
  cp .env.example .env
  sed -i "s|^EW_IP=.*|EW_IP=$EWIP|; s|^EW_SUBNET=.*|EW_SUBNET=$NET|; s|^EW_GATEWAY=.*|EW_GATEWAY=$GW|; s|^EW_PARENT_IF=.*|EW_PARENT_IF=$IF|; s|^EW_SETUP_TOKEN=.*|EW_SETUP_TOKEN=$TOKEN|" .env
  chmod 600 .env
fi

PROFILE=""
read -rp "Honeygain- und EarnApp-Container auch starten? [j/N] " earn
if [[ "${earn,,}" == j* ]]; then
  grep -q '^HG_EMAIL=.\+' .env || { read -rp "Honeygain E-Mail: " HG; read -rsp "Honeygain Passwort: " HP; echo
    grep -v -E '^HG_(EMAIL|PASSWORD)=' .env > .env.tmp
    printf "HG_EMAIL=%s\nHG_PASSWORD='%s'\n" "$HG" "$HP" >> .env.tmp
    mv .env.tmp .env; chmod 600 .env; }
  grep -q '^EARNAPP_UUID=.\+' .env || { UUID="sdk-node-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    sed -i "s|^EARNAPP_UUID=.*|EARNAPP_UUID=$UUID|" .env; }
  PROFILE="--profile earners"
fi

say "Baue und starte EarnWatch (dauert 1–2 Minuten)"
docker compose $PROFILE up -d --build

set -a; . ./.env; set +a
say "Fertig!"
echo "  Öffne von PC/Handy:   https://$EW_IP   (Zertifikatswarnung einmal bestätigen)"
echo "  Einrichtungs-Token:   $EW_SETUP_TOKEN"
echo "  (steht auch in $DIR/.env)"
[ -n "$PROFILE" ] && echo "  EarnApp-Gerät verknüpfen: https://earnapp.com/r/$EARNAPP_UUID"
echo "  Hinweis: Vom Server selbst ist die IP nicht erreichbar – das ist normal."
