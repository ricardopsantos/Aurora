#!/usr/bin/env bash
# summary: restarts the aurora --web unit after its code changed (run by the post-merge hook or by hand) and confirms the new version answers on /health.
set -euo pipefail
UNIT="${AURORA_WEB_UNIT:-aurora-web.service}"
PORT="${AURORA_WEB_PORT:-9592}"
HOST="${AURORA_WEB_HOST:-127.0.0.1}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
want="$("$REPO/.venv/bin/python" -c 'import aurora; print(aurora.__version__)' 2>/dev/null || echo '?')"
[ "$(systemctl show -p LoadState --value "$UNIT" 2>/dev/null)" = loaded ] || { echo "restart-web: no unit $UNIT here, nothing to do"; exit 0; }
sudo -n systemctl restart "$UNIT"
for _ in $(seq 1 30); do
  got="$(curl -s -m 2 "http://$HOST:$PORT/health" | "$REPO/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
  if [ -n "$got" ]; then
    [ "$got" = "$want" ] && { echo "restart-web: $UNIT up, version $got"; exit 0; }
    echo "restart-web: $UNIT answers version $got, expected $want" >&2; exit 1
  fi
  sleep 1
done
echo "restart-web: $UNIT did not answer on :$PORT/health within 30 s" >&2
exit 1
