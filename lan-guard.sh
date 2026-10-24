#!/usr/bin/env bash
# EarnWatch LAN-Sperre: Die Earn-Apps (Honeygain, EarnApp, Pawns, Earn.fm) leiten fremden
# Internetverkehr über deinen Anschluss. Diese Regeln verhindern, dass aus ihren Containern
# heraus Geräte in deinem Heimnetz erreicht werden (Router, Proxmox, NAS, ...).
# Internet bleibt erreichbar.  Aufruf: lan-guard.sh [apply|remove|status]
set -euo pipefail
SUBNET="172.31.250.0/24"     # fest vergebenes Docker-Netz "earners" (siehe docker-compose.yml)
RANGES="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 224.0.0.0/4"
TAG="earnwatch-lan-guard"

command -v iptables >/dev/null || { echo "iptables fehlt – LAN-Sperre nicht möglich"; exit 1; }

remove() {
  while read -r n; do iptables -D DOCKER-USER "$n" 2>/dev/null || true
  done < <(iptables -L DOCKER-USER --line-numbers -n 2>/dev/null | awk -v t="$TAG" '$0 ~ t {print $1}' | sort -rn)
}

case "${1:-apply}" in
  apply)
    iptables -L DOCKER-USER -n >/dev/null 2>&1 || iptables -N DOCKER-USER
    remove
    for r in $RANGES; do
      iptables -I DOCKER-USER -s "$SUBNET" -d "$r" -m comment --comment "$TAG" -j DROP
    done
    # Antworten auf bestehende Verbindungen (z. B. Docker-DNS) erlauben – ganz oben
    iptables -I DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$TAG" -j RETURN
    echo "LAN-Sperre aktiv für $SUBNET"
    ;;
  remove) remove; echo "LAN-Sperre entfernt" ;;
  status) iptables -L DOCKER-USER -n -v | grep "$TAG" || echo "keine Regeln aktiv" ;;
  *) echo "Aufruf: $0 [apply|remove|status]"; exit 2 ;;
esac
