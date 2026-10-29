#!/usr/bin/env bash
# Desktop-style launcher (macOS / Linux): starts the services, then opens a
# standalone app window via `chrome --app`, falling back to Edge, then to the
# default browser.
#
# All the logic lives in scripts/dev.py -- this file is only an entry point, so
# there is exactly one copy of the startup logic to maintain.

set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi

echo "Starting Daily Planner ...  app window :5173  backend :8000"
echo "(Ctrl+C to stop the servers)"
echo

exec "$PY" scripts/dev.py --app
