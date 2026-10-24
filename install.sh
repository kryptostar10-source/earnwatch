#!/usr/bin/env bash
# EarnWatch Installer – fragt die Netzwerkdaten ab, schreibt die .env und startet EarnWatch.
# Aufruf im geklonten Ordner:  bash install.sh
set -euo pipefail
umask 077   # .env mit Passwörtern nur für root lesbar
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

setenv(){ # setenv NAME WERT  -> sicher in .env schreiben (ohne sed, Sonderzeichen erlaubt)
  grep -v -E "^$1=" .env > .env.tmp || true
  if [[ "$2" != *"'"* ]]; then printf "%s='%s'\n" "$1" "$2" >> .env.tmp
  else v=${2//\\/\\\\}; v=${v//\"/\\\"}; v=${v//\$/\$\$}; printf '%s="%s"\n' "$1" "$v" >> .env.tmp; fi
  mv .env.tmp .env; chmod 600 .env
}
PROFILES=$(grep -E '^COMPOSE_PROFILES=' .env | cut -d= -f2- | tr -d "'\"" || true)
add_profile(){ case ",$PROFILES," in *",$1,"*) ;; *) PROFILES="${PROFILES:+$PROFILES,}$1";; esac; }

read -rp "Earn-Apps als Container auf diesem Server mitlaufen lassen? [j/N] " earn
if [[ "${earn,,}" == j* ]]; then
  read -rp "  Honeygain starten? [j/N] " a
  if [[ "${a,,}" == j* ]]; then
    grep -q "^HG_EMAIL=.\+" .env || { read -rp "    Honeygain E-Mail: " v; setenv HG_EMAIL "$v"; read -rsp "    Honeygain Passwort: " v; echo; setenv HG_PASSWORD "$v"; }
    add_profile honeygain
  fi
  read -rp "  EarnApp starten? [j/N] " a
  if [[ "${a,,}" == j* ]]; then
    grep -q "^EARNAPP_UUID=.\+" .env || setenv EARNAPP_UUID "sdk-node-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    add_profile earnapp
  fi
  read -rp "  Pawns.app starten? [j/N] " a
  if [[ "${a,,}" == j* ]]; then
    grep -q "^PAWNS_EMAIL=.\+" .env || { read -rp "    Pawns E-Mail: " v; setenv PAWNS_EMAIL "$v"; read -rsp "    Pawns Passwort: " v; echo; setenv PAWNS_PASSWORD "$v"; }
    add_profile pawns
  fi
  read -rp "  Earn.fm starten? [j/N] " a
  if [[ "${a,,}" == j* ]]; then
    grep -q "^EARNFM_TOKEN=.\+" .env || { echo "    API-Key: app.earn.fm -> Profil -> Account Settings"; read -rsp "    Earn.fm API-Key: " v; echo; setenv EARNFM_TOKEN "$v"; }
    add_profile earnfm
  fi
  setenv COMPOSE_PROFILES "$PROFILES"
fi

if [ -n "$(grep -E "^COMPOSE_PROFILES=.+" .env | grep -v "=''$" || true)" ]; then
  say "LAN-Sperre für die Earn-Apps"
  echo "  Die Earn-Apps leiten fremden Internetverkehr über deinen Anschluss."
  echo "  Die Sperre verhindert, dass sie Geräte in deinem Heimnetz erreichen (Router, Proxmox, NAS)."
  read -rp "  LAN-Sperre einrichten? (empfohlen) [J/n] " lg
  if [[ "${lg,,}" != n* ]]; then
    cat > /etc/systemd/system/earnwatch-lan-guard.service <<UNIT
[Unit]
Description=EarnWatch: Earn-App-Container vom Heimnetz abschotten
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$DIR/lan-guard.sh apply
ExecStop=$DIR/lan-guard.sh remove

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload && systemctl enable --now earnwatch-lan-guard.service \
      && echo "  LAN-Sperre aktiv (bleibt auch nach Neustart)" \
      || echo "  Hinweis: LAN-Sperre konnte nicht aktiviert werden (iptables/systemd fehlt?)"
  fi
fi

say "Baue und starte EarnWatch (dauert 1–2 Minuten)"
# Älteres Earn-App-Netz ohne festes Subnetz? Dann einmal neu anlegen lassen.
OLDNET=$(docker network inspect earnwatch_earners -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null || true)
if [ -n "$OLDNET" ] && [ "$OLDNET" != "172.31.250.0/24" ]; then docker compose down; fi
docker compose up -d --build
systemctl is-enabled earnwatch-lan-guard.service >/dev/null 2>&1 && systemctl restart earnwatch-lan-guard.service || true

getenv(){ grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//'; }
EW_IP=$(getenv EW_IP); EW_SETUP_TOKEN=$(getenv EW_SETUP_TOKEN); EARNAPP_UUID=$(getenv EARNAPP_UUID); COMPOSE_PROFILES=$(getenv COMPOSE_PROFILES)
say "Fertig!"
echo "  Öffne von PC/Handy:   https://$EW_IP   (Zertifikatswarnung einmal bestätigen)"
echo "  Einrichtungs-Token:   $EW_SETUP_TOKEN"
echo "  (steht auch in $DIR/.env)"
case ",${COMPOSE_PROFILES:-}," in *,earnapp,*) echo "  EarnApp-Gerät verknüpfen: https://earnapp.com/r/$EARNAPP_UUID";; esac
echo "  Wipter: Kontostand trägst du direkt auf der Übersicht ein."
echo "  Hinweis: Vom Server selbst ist die IP nicht erreichbar – das ist normal."
