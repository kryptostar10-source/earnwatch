#!/usr/bin/env bash
# EarnWatch Container-Wächter (läuft alle 5 Minuten per systemd-Timer auf dem Host):
#  - startet gestoppte, abgestürzte oder "unhealthy" Container wieder
#  - erkennt Absturz-Schleifen (Container startet ständig neu)
#  - meldet alles an EarnWatch (-> Handy-Benachrichtigung + Anzeige auf der Übersicht)
# Bewusst pausieren: Dienst in .env bei HC_IGNORE eintragen, z. B. HC_IGNORE=pawns,earnfm
# Der EarnWatch-Container bekommt dafür KEINEN Zugriff auf Docker – nur dieses Skript (root) hat ihn.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 0
[ -f .env ] || exit 0
STATE_DIR=/var/lib/earnwatch-health
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
IGNORE=",$(grep -E '^HC_IGNORE=' .env | tail -1 | cut -d= -f2- | tr -d "\"' "),"

json_escape(){ printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
services=() events=()
while read -r s; do
  [[ "$s" =~ ^[a-z0-9_-]{1,40}$ ]] || continue
  case "$IGNORE" in *",$s,"*) continue;; esac
  # Von zugangsdaten.sh vorübergehend pausiert (z. B. Login-Sperre beim Anbieter)?
  if [ -f "$STATE_DIR/$s.pause" ] && [ "$(cat "$STATE_DIR/$s.pause" 2>/dev/null || echo 0)" -gt "$(date +%s)" ] 2>/dev/null; then continue; fi
  rm -f "$STATE_DIR/$s.pause"
  cid=$(docker compose ps -a -q "$s" 2>/dev/null | head -1)
  if [ -z "$cid" ]; then state=missing; restarts=0; health=""
  else
    state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)
    restarts=$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || echo "")
  fi
  prev_restarts=$(cat "$STATE_DIR/$s.restarts" 2>/dev/null || echo "$restarts")
  prev_state=$(cat "$STATE_DIR/$s.state" 2>/dev/null || echo running)
  action=""
  if [ "$state" = "restarting" ]; then
    # Docker startet ihn schon selbst immer wieder neu (Absturz-Schleife) -> nur EINMAL melden
    [ "$prev_state" = "restarting" ] || events+=("{\"name\":\"$s\",\"kind\":\"down\",\"detail\":\"restarting\",\"action\":\"loop\"}")
  elif [ "$state" != "running" ] || [ "$health" = "unhealthy" ]; then
    if [ "$health" = "unhealthy" ]; then docker restart "$cid" >/dev/null 2>&1 && action=restarted
    else docker compose up -d "$s" >/dev/null 2>&1 && action=started; fi
    events+=("{\"name\":\"$s\",\"kind\":\"down\",\"detail\":\"$(json_escape "$state${health:+/$health}")\",\"action\":\"$action\"}")
    state_after=$(docker compose ps -q "$s" 2>/dev/null | head -1 | xargs -r docker inspect -f '{{.State.Status}}' 2>/dev/null || echo "$state")
    state="$state_after"
  elif [ "$prev_state" != "running" ]; then
    events+=("{\"name\":\"$s\",\"kind\":\"recovered\",\"detail\":\"\",\"action\":\"\"}")
  fi
  if [ "$state" != "restarting" ] && [ "$restarts" -gt "$prev_restarts" ] 2>/dev/null; then
    events+=("{\"name\":\"$s\",\"kind\":\"crashed\",\"detail\":\"$((restarts - prev_restarts))\",\"action\":\"\"}")
  fi
  echo "$restarts" > "$STATE_DIR/$s.restarts"; echo "$state" > "$STATE_DIR/$s.state"
  services+=("{\"name\":\"$s\",\"state\":\"$(json_escape "$state")\",\"health\":\"$(json_escape "$health")\",\"restarts\":$restarts}")
done < <(docker compose config --services 2>/dev/null)

# Noch nicht zugestellte Meldungen (z. B. als EarnWatch selbst down war) mitschicken
QUEUE="$STATE_DIR/queue"
[ -f "$QUEUE" ] && while read -r e; do [ -n "$e" ] && events+=("$e"); done < "$QUEUE"
join(){ local IFS=,; echo "$*"; }
PAYLOAD="{\"services\":[$(join "${services[@]}")],\"events\":[$(join "${events[@]}")]}"
if docker compose exec -T earnwatch python -m earnwatch.hostevent "$PAYLOAD" >/dev/null 2>&1; then
  rm -f "$QUEUE"
else
  # EarnWatch gerade nicht erreichbar -> Meldungen fürs nächste Mal aufheben (max. 50)
  printf '%s\n' "${events[@]}" | tail -50 > "$QUEUE.tmp" && mv "$QUEUE.tmp" "$QUEUE"
fi
exit 0
