#!/usr/bin/env bash
# summary: installs a git post-merge hook in this clone that runs deploy/restart-web.sh when a pull changed aurora/ (set AURORA_WEB_UNIT/PORT in the hook's environment for non-default units).
set -euo pipefail
UNIT="${1:-aurora-web.service}"; PORT="${2:-9592}"; HOST="${3:-127.0.0.1}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO/.git/hooks/post-merge"
cat > "$HOOK" <<HOOK
#!/usr/bin/env bash
# summary: after a pull, restart aurora --web if its code changed (installed by deploy/install-hooks.sh).
if git diff --name-only ORIG_HEAD HEAD 2>/dev/null | grep -q '^aurora/'; then
  AURORA_WEB_UNIT="\${AURORA_WEB_UNIT:-$UNIT}" AURORA_WEB_PORT="\${AURORA_WEB_PORT:-$PORT}" AURORA_WEB_HOST="\${AURORA_WEB_HOST:-$HOST}" "$REPO/deploy/restart-web.sh" || echo "post-merge: aurora web restart failed" >&2
fi
HOOK
chmod +x "$HOOK"
echo "installed $HOOK"
