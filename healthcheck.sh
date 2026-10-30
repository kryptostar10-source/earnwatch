#!/usr/bin/env bash
# EarnWatch Container-Wächter (läuft alle 5 Minuten per systemd-Timer auf dem Host):
#  - zeigt ALLE Docker-Container auf dem Server in der EarnWatch-Oberfläche an
#  - startet NUR die eigenen EarnWatch-Container wieder (gestoppt/abgestürzt/unhealthy)
#    und meldet das aufs Handy. Fremde Container (z. B. Portainer) werden nur angezeigt,
#    nie angefasst.
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

# IDs der eigenen EarnWatch-Container (dieses Compose-Projekts) merken
declare -A OWN
while read -r id; do [ -n "$id" ] && OWN["$id"]=1; done < <(docker compose ps -aq 2>/dev/null)

services=() events=()
# Alle Container auf dem Server durchgehen (max. 80)
while read -r cid; do
  [ -n "$cid" ] || continue
  name=$(docker inspect -f '{{.Name}}' "$cid" 2>/dev/null | sed 's#^/##'); [ -n "$name" ] || continue
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || continue
  svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$cid" 2>/dev/null)
  key="${svc:-$name}"
  case "$IGNORE" in *",$key,"*) continue;; esac
  state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)
  restarts=$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || echo "")
  own=0; [ -n "${OWN[$cid]:-}" ] && own=1

  if [ "$own" = 1 ]; then
    # Von zugangsdaten.sh vorübergehend pausiert (z. B. Login-Sperre beim Anbieter)?
    if [ -f "$STATE_DIR/$svc.pause" ] && [ "$(cat "$STATE_DIR/$svc.pause" 2>/dev/null || echo 0)" -gt "$(date +%s)" ] 2>/dev/null; then
      services+=("{\"name\":\"$(json_escape "$name")\",\"state\":\"paused\",\"health\":\"\",\"restarts\":$restarts,\"own\":1}")
      continue
    fi
    rm -f "$STATE_DIR/$svc.pause"
    prev_restarts=$(cat "$STATE_DIR/$svc.restarts" 2>/dev/null || echo "$restarts")
    prev_state=$(cat "$STATE_DIR/$svc.state" 2>/dev/null || echo running)
    action=""
    if [ "$state" = "restarting" ]; then
      [ "$prev_state" = "restarting" ] || events+=("{\"name\":\"$(json_escape "$svc")\",\"kind\":\"down\",\"detail\":\"restarting\",\"action\":\"loop\"}")
    elif [ "$state" != "running" ] || [ "$health" = "unhealthy" ]; then
      if [ "$health" = "unhealthy" ]; then docker restart "$cid" >/dev/null 2>&1 && action=restarted
      else docker compose up -d "$svc" >/dev/null 2>&1 && action=started; fi
      events+=("{\"name\":\"$(json_escape "$svc")\",\"kind\":\"down\",\"detail\":\"$(json_escape "$state${health:+/$health}")\",\"action\":\"$action\"}")
      state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo "$state")
    elif [ "$prev_state" != "running" ]; then
      events+=("{\"name\":\"$(json_escape "$svc")\",\"kind\":\"recovered\",\"detail\":\"\",\"action\":\"\"}")
    fi
    if [ "$state" != "restarting" ] && [ "$restarts" -gt "$prev_restarts" ] 2>/dev/null; then
      events+=("{\"name\":\"$(json_escape "$svc")\",\"kind\":\"crashed\",\"detail\":\"$((restarts - prev_restarts))\",\"action\":\"\"}")
    fi
    echo "$restarts" > "$STATE_DIR/$svc.restarts"; echo "$state" > "$STATE_DIR/$svc.state"
  fi
  services+=("{\"name\":\"$(json_escape "$name")\",\"state\":\"$(json_escape "$state")\",\"health\":\"$(json_escape "$health")\",\"restarts\":$restarts,\"own\":$own}")
done < <(docker ps -aq 2>/dev/null | head -80)

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
