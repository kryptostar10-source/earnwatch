#!/usr/bin/env bash
# EarnWatch aus einem Backup wiederherstellen.
# Aufruf: sudo bash /opt/earnwatch/restore.sh /var/backups/earnwatch/earnwatch-JJJJ-MM-TT_HHMM.tar.gz
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen"; exit 1; }
FILE="${1:-}"
if [ -z "$FILE" ]; then
  echo "Verfügbare Backups:"; ls -1t /var/backups/earnwatch/earnwatch-*.tar.gz 2>/dev/null || echo "  (keine)"
  echo "Aufruf: sudo bash restore.sh <Datei>"; exit 1
fi
[ -f "$FILE" ] || { echo "Datei nicht gefunden: $FILE"; exit 1; }
gzip -t "$FILE"
tar tzf "$FILE" | grep -qx "earnwatch.db" || { echo "Das ist kein EarnWatch-Backup"; exit 1; }
read -rp "Aktuelle Daten werden durch das Backup ersetzt. Fortfahren? [j/N] " ok
[[ "${ok,,}" == j* ]] || exit 1
VOL="$(docker compose config --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["earnwatch_data"]["name"])' 2>/dev/null || echo earnwatch_earnwatch_data)"
docker compose stop earnwatch
docker run --rm -v "$VOL":/d -v "$(realpath "$FILE")":/b.tgz:ro alpine sh -c \
  'rm -f /d/earnwatch.db-wal /d/earnwatch.db-shm && tar xzf /b.tgz -C /d earnwatch.db .fernet.key .session.key 2>/dev/null || tar xzf /b.tgz -C /d; chown -R 10001:10001 /d && chmod 600 /d/earnwatch.db /d/.fernet.key /d/.session.key 2>/dev/null; true'
docker compose start earnwatch
echo "Wiederhergestellt aus $FILE"
