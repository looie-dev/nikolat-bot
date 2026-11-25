#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: ./deploy/deploy.sh root@<VPS_IP> [SSH_PORT]"
  exit 1
fi

TARGET="$1"
PORT="${2:-22}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
REMOTE_DIR="/opt/xauusd-bot"

echo "==> 1. Checking Python 3 on $TARGET..."
ssh -p "$PORT" "$TARGET" "apt-get update -qq && apt-get install -y -qq python3 >/dev/null || true; python3 --version"

echo "==> 2. Syncing project files to $TARGET:$REMOTE_DIR..."
ssh -p "$PORT" "$TARGET" "mkdir -p $REMOTE_DIR"
rsync -avz -e "ssh -p $PORT" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'data/' \
  "$PROJECT_DIR/" "$TARGET:$REMOTE_DIR/"

echo "==> 3. Installing and starting systemd service..."
ssh -p "$PORT" "$TARGET" "cp $REMOTE_DIR/deploy/xauusd-bot.service /etc/systemd/system/xauusd-bot.service && systemctl daemon-reload && systemctl enable xauusd-bot && systemctl restart xauusd-bot && systemctl status xauusd-bot --no-pager"

echo ""
echo "==> Done! Xem log realtime bang lenh:"
echo "    ssh -p $PORT $TARGET 'journalctl -u xauusd-bot -f'"
