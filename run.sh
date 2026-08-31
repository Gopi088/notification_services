#!/usr/bin/env bash
# Starts the API on $HOST:$PORT (default 127.0.0.1:8000).
#
# HOST and PORT are read from .env (or the environment). Examples:
#   HOST=0.0.0.0 PORT=8080 ./run.sh
#
# Linux / WSL notes:
# - Uses venv/bin/uvicorn directly, so the venv does NOT need to be activated.
# - --reload-dir app makes the watcher scan only the source directory. It
#   never traverses venv/, .git/, or the nested notification-service/ copy,
#   which previously exhausted the inotify watch table.
# - On WSL, projects under /mnt/c live on a 9p mount where inotify fails with
#   "Cannot allocate memory (os error 12)". WATCHFILES_FORCE_POLLING switches
#   watchfiles to a polling watcher that works reliably on 9p.
# - USE_RELOAD=1 enables auto-reload for development. The CLI (notification_service.py)
#   starts the server WITHOUT reload: polling watchers on the 9p mount cause
#   heavy disk I/O that makes WSL2 appear frozen.
set -euo pipefail
cd "$(dirname "$0")"

BIN=venv/bin/uvicorn
if [ ! -x "$BIN" ]; then
  echo "venv not found - creating it and installing dependencies..." >&2
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
fi

# Read HOST/PORT from .env (KEY=VALUE lines), falling back to defaults.
_read_env() {
  local key="$1" default="$2"
  local val
  val=$(grep -E "^${key}=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' "\r' || true)
  if [ -z "$val" ]; then
    val="$default"
  fi
  printf '%s' "$val"
}

HOST=$(_read_env HOST "127.0.0.1")
PORT=$(_read_env PORT "8000")

case "$PORT" in
  ''|*[!0-9]*)
    echo "ERROR: PORT must be a number, got: '$PORT'" >&2
    echo "       Set PORT in .env (or export it) and re-run." >&2
    exit 1
    ;;
esac

# Fail fast with a clear message when the port is already occupied.
if (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null; then
  exec 3>&- 3<&-
  echo "ERROR: Port $PORT on host $HOST is already in use." >&2
  echo "" >&2
  echo "  The notification service is configured to listen on $HOST:$PORT." >&2
  echo "  Either stop the process using that port, or change the address:" >&2
  echo "" >&2
  echo "    # in .env" >&2
  echo "    HOST=127.0.0.1" >&2
  echo "    PORT=8080" >&2
  echo "" >&2
  echo "    # or as an env override" >&2
  echo "    HOST=127.0.0.1 PORT=8080 ./run.sh" >&2
  exit 1
fi

ARGS=(app.main:app --host "$HOST" --port "$PORT")
if [ "${USE_RELOAD:-0}" = "1" ]; then
  if grep -qi "microsoft" /proc/version 2>/dev/null; then
    export WATCHFILES_FORCE_POLLING=true
  fi
  ARGS+=(--reload --reload-dir app --reload-delay 3)
fi

echo "Starting notification service on http://$HOST:$PORT" >&2
exec "$BIN" "${ARGS[@]}"
