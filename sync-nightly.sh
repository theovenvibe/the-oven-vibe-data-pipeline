#!/usr/bin/env bash
# Pull everything into warehouse.duckdb, quietly, whenever the laptop is on and
# online. Wired to a systemd timer (see docs/NIGHTLY_SYNC.md) rather than cron
# so a missed run — laptop shut, no internet — is caught the next time the
# machine is awake instead of being skipped until tomorrow.
#
# Exits 0 when there is no connection: "not online right now" is not a failure
# worth an alert at 2am.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

if ! curl -fsS --max-time 10 -o /dev/null https://api.theovenvibe.com/availability; then
  echo "$(date -Is) offline or the Worker is unreachable — skipping"
  exit 0
fi

[ -f .env ] && set -a && . ./.env && set +a

echo "$(date -Is) syncing"
./.venv/bin/python main.py
echo "$(date -Is) done"
