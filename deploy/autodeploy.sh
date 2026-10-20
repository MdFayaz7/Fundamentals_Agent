#!/usr/bin/env bash
# Auto-deploy: if GitHub has new commits on main, fast-forward, reinstall deps when they changed, and
# restart the bot (systemd restarts it after the process exits). Run every 5 min from cron:
#   */5 * * * * $HOME/resultagent/deploy/autodeploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=data/autodeploy.log
mkdir -p data

git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && exit 0

OLD=$(git rev-parse --short HEAD)
if ! git merge -q --ff-only origin/main; then
  echo "$(date '+%F %T') skip: local changes block fast-forward from $OLD" >> "$LOG"
  exit 1
fi
NEW=$(git rev-parse --short HEAD)
if git diff --name-only "$OLD" "$NEW" | grep -q '^pyproject.toml$'; then
  .venv/bin/pip install -q .
fi
PID=$(systemctl show -p MainPID --value resultagent || echo 0)
[ "$PID" != "0" ] && kill "$PID"
echo "$(date '+%F %T') deployed $OLD -> $NEW" >> "$LOG"
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
