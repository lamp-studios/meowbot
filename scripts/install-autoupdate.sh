#!/usr/bin/env bash
# installs a systemd timer that runs scripts/update.sh every few minutes
# usage: ./scripts/install-autoupdate.sh [interval]   (default 5min, e.g. 1min, 15min, 1h)
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
PROJECT_DIR="$(pwd)"
INTERVAL="${1:-5min}"

if [ "$(id -u)" -eq 0 ]; then
    echo "don't run this as root, it'll sudo when it needs to"
    exit 1
fi

chmod +x scripts/update.sh

# runs as root so it can restart the bot, update.sh drops back to your user for git/uv
sudo tee /etc/systemd/system/meowbot-update.service >/dev/null <<EOF
[Unit]
Description=MeowBot auto updater
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=${PROJECT_DIR}/scripts/update.sh
EOF

sudo tee /etc/systemd/system/meowbot-update.timer >/dev/null <<EOF
[Unit]
Description=Check for MeowBot updates every ${INTERVAL}

[Timer]
OnBootSec=2min
OnUnitActiveSec=${INTERVAL}
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now meowbot-update.timer

echo "auto updates on, checking every ${INTERVAL}"
echo "   journalctl -u meowbot-update -f             # updater logs"
echo "   sudo systemctl start meowbot-update         # check for updates right now"
echo "   sudo systemctl disable --now meowbot-update.timer   # turn auto updates off"
