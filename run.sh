#!/usr/bin/env bash
# Starts the API on http://127.0.0.1:8000 with auto-reload.
#
# Linux / WSL notes:
# - Uses venv/bin/uvicorn directly, so the venv does NOT need to be activated.
# - --reload-dir app makes the watcher scan only the source directory. It
#   never traverses venv/, .git/, or the nested notification-service/ copy,
#   which previously exhausted the inotify watch table.
# - On WSL, projects under /mnt/c live on a 9p mount where inotify fails with
#   "Cannot allocate memory (os error 12)". WATCHFILES_FORCE_POLLING switches
#   watchfiles to a polling watcher that works reliably on 9p.
set -euo pipefail
cd "$(dirname "$0")"

BIN=venv/bin/uvicorn
if [ ! -x "$BIN" ]; then
  echo "venv not found - creating it and installing dependencies..." >&2
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
fi

if grep -qi "microsoft" /proc/version 2>/dev/null; then
  export WATCHFILES_FORCE_POLLING=true
fi

exec "$BIN" app.main:app --reload --reload-dir app --host 127.0.0.1 --port 8000