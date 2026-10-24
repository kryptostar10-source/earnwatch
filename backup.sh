#!/usr/bin/env bash
# EarnWatch-Backup: Datenbank (konsistente Kopie) + Schlüssel als .tar.gz
# Aufruf: sudo bash /opt/earnwatch/backup.sh      (läuft auch automatisch jede Nacht)
# Ziel:   /var/backups/earnwatch (nur root lesbar), die letzten 7 bleiben erhalten.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
[ "$(id -u)" -eq 0 ] || { echo "Bitte mit sudo ausführen"; exit 1; }
DEST="${EW_BACKUP_DIR:-/var/backups/earnwatch}"
KEEP="${EW_BACKUP_KEEP:-7}"
mkdir -p "$DEST" && chmod 700 "$DEST"
FILE="$DEST/earnwatch-$(date +%Y-%m-%d_%H%M).tar.gz"

# Die Kopie wird IM Container erstellt (SQLite-Backup-API = konsistent auch während des Betriebs)
docker compose exec -T earnwatch python - > "$FILE.part" <<'PY'
import io, sqlite3, sys, tarfile, time
src = sqlite3.connect("/data/earnwatch.db")
mem = sqlite3.connect(":memory:")
src.backup(mem)
files = {"earnwatch.db": mem.serialize()}
for name in (".fernet.key", ".session.key"):
    try:
        files[name] = open(f"/data/{name}", "rb").read()
    except FileNotFoundError:
        pass
out = io.BytesIO()
with tarfile.open(fileobj=out, mode="w:gz") as tar:
    for name, data in files.items():
        info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600; info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
sys.stdout.buffer.write(out.getvalue())
PY
gzip -t "$FILE.part"
mv "$FILE.part" "$FILE"
echo "Backup gespeichert: $FILE ($(du -h "$FILE" | cut -f1))"
# Alte Backups aufräumen
ls -1t "$DEST"/earnwatch-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
