#!/usr/bin/env bash
# EarnWatch aktualisieren – Code UND Sicherheits-Updates der Basis-Images.
# Aufruf:  sudo bash /opt/earnwatch/update.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen"; exit 1; }
echo "==> Sicherheitskopie vor dem Update"
bash ./backup.sh || echo "   (Backup übersprungen – läuft EarnWatch?)"
echo "==> Neuer Code von GitHub"
git pull --ff-only
echo "==> Aktuelle Images holen (Caddy, Tailscale, Earn-Apps, Python-Basis)"
docker compose pull --ignore-buildable 2>/dev/null || docker compose pull || true
docker compose build --pull
echo "==> Neu starten"
docker compose up -d --remove-orphans
systemctl is-enabled earnwatch-lan-guard.service >/dev/null 2>&1 && systemctl restart earnwatch-lan-guard.service || true
docker image prune -f >/dev/null
echo "==> Fertig. Status:"
docker compose ps
