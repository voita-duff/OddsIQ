#!/usr/bin/env bash
#
# start.sh — one-command local launcher.
#
# Brings up the read-only dashboard in the background, opens it in your
# browser, then runs the pipeline in the foreground. Press Ctrl-C once to stop
# both: the pipeline shuts down gracefully and the dashboard this script
# started is torn down with it.
#
# Usage:
#   ./scripts/start.sh            # or: make start
#   ODDSIQ_DASHBOARD_PORT=9000 ./scripts/start.sh
#
set -eo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ODDSIQ_DASHBOARD_PORT:-8080}"
URL="http://127.0.0.1:${PORT}"

cd "$REPO"

if [ ! -f .venv/bin/activate ]; then
  echo "No virtualenv at $REPO/.venv — create it first:" >&2
  echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  exit 1
fi

# Guard against a second pipeline: two instances would double the Odds API
# spend and both write the same SQLite DB. 'oddsiq dashboard' ends in
# 'dashboard', so this pattern only matches a bare pipeline process.
if pgrep -f "/oddsiq$" >/dev/null 2>&1; then
  echo "A pipeline already seems to be running. Stop it first (Ctrl-C in its" >&2
  echo "terminal) so you don't run two instances against the same database." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

dash_pid=""
cleanup() {
  if [ -n "$dash_pid" ] && kill -0 "$dash_pid" 2>/dev/null; then
    echo
    echo "Stopping dashboard (pid $dash_pid)…"
    kill "$dash_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Reuse an already-running dashboard if present; otherwise start one.
if curl -sf "$URL" >/dev/null 2>&1; then
  echo "Dashboard already up at $URL — reusing it."
else
  mkdir -p logs
  echo "Starting dashboard → $URL  (output: logs/dashboard.log)"
  oddsiq dashboard --port "$PORT" >>logs/dashboard.log 2>&1 &
  dash_pid=$!
fi

# Wait up to ~15s for it to answer, then open the browser.
for _ in {1..30}; do
  curl -sf "$URL" >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -sf "$URL" >/dev/null 2>&1; then
  open "$URL" 2>/dev/null || echo "Open $URL in your browser."
else
  echo "Dashboard didn't answer in time — check logs/dashboard.log" >&2
fi

echo "Starting pipeline — Ctrl-C once to stop both."
echo "──────────────────────────────────────────────"
oddsiq
