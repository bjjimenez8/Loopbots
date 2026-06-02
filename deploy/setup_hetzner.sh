#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${LOOPBOTS_REPO_URL:-https://github.com/bjjimenez8/Loopbots.git}"
APP_DIR="${LOOPBOTS_APP_DIR:-/opt/loopbots}"
CONFIG_DIR="${LOOPBOTS_CONFIG_DIR:-/etc/loopbots}"
STATE_DIR="${LOOPBOTS_STATE_DIR:-/var/lib/loopbots}"
LOG_DIR="${LOOPBOTS_LOG_DIR:-/var/log/loopbots}"
SERVICE_PATH="/etc/systemd/system/loopbots.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this setup script as root, for example: sudo bash deploy/setup_hetzner.sh"
  exit 1
fi

apt-get update
apt-get install -y git python3 python3-venv python3-pip

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR"

if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin main
  git -C "$APP_DIR" reset --hard origin/main
else
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
  cp "$APP_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
  python3 - <<'PY'
from pathlib import Path

path = Path("/etc/loopbots/config.yaml")
text = path.read_text(encoding="utf-8")
text = text.replace('active_trades_file: "data/active_trades.json"', 'active_trades_file: "/var/lib/loopbots/active_trades.json"')
text = text.replace('trade_history_file: "data/trade_history.csv"', 'trade_history_file: "/var/lib/loopbots/trade_history.csv"')
text = text.replace('log_file: "logs/loopbots.log"', 'log_file: "/var/log/loopbots/loopbots.log"')
path.write_text(text, encoding="utf-8")
PY
  echo "Created $CONFIG_DIR/config.yaml."
  echo "Edit it with your Telegram token/chat ID before starting the service."
fi

sed "s#/opt/loopbots#$APP_DIR#g" "$APP_DIR/deploy/loopbots.service" > "$SERVICE_PATH"
systemctl daemon-reload
systemctl enable loopbots

if grep -q "YOUR_TELEGRAM" "$CONFIG_DIR/config.yaml"; then
  echo "Loopbots service is installed but not started because Telegram values are still placeholders."
  echo "Edit $CONFIG_DIR/config.yaml, then run: systemctl restart loopbots"
  exit 0
fi

systemctl restart loopbots
systemctl --no-pager --full status loopbots
