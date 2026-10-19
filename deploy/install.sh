#!/usr/bin/env bash
# Install / update the Results Flash Agent as a systemd service. Run from the project folder.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11+ required"; exit 1; }
# Debian/Ubuntu ship venv without ensurepip; install it once if missing.
python3 -c 'import ensurepip' 2>/dev/null || { sudo apt-get update -q && sudo apt-get install -y -q python3-venv; }

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q .

[ -f config.toml ] || cp config.example.toml config.toml
if [ ! -f .env ]; then cp .env.example .env; echo ">> Edit $DIR/.env (Telegram token, chat id, LLM keys), then re-run."; fi
chmod 600 .env
mkdir -p data

sed -e "s|__USER__|$(id -un)|" -e "s|__DIR__|$DIR|g" deploy/resultagent.service \
  | sudo tee /etc/systemd/system/resultagent.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now resultagent
sudo systemctl restart resultagent
echo "Installed. Logs: journalctl -u resultagent -f"
echo "Tip: cap log size with SystemMaxUse=100M in /etc/systemd/journald.conf"
