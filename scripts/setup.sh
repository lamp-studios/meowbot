#!/usr/bin/env bash
# meowbot setup script for debian/ubuntu
# installs system deps, syncs the venv with uv, and optionally installs a systemd service for 24/7 uptime
set -euo pipefail

# always run from the project root (the folder above this script)
cd "$(dirname "$(readlink -f "$0")")/.."
PROJECT_DIR="$(pwd)"
RUN_USER="$(id -un)"
SERVICE_NAME="meowbot"
ENTRY="main.py"

step() { printf '\n\033[35m>> %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32m%s\033[0m\n' "$1"; }
warn() { printf '   \033[33m%s\033[0m\n' "$1"; }
err()  { printf '   \033[31m%s\033[0m\n' "$1"; }

printf '\033[36m=== meowbot setup ===\033[0m\n'

if [ "$(id -u)" -eq 0 ]; then
    err "don't run this as root, run it as the user that should own the bot (it'll sudo when needed)"
    exit 1
fi

# 1. system packages
step "installing system packages (needs sudo)"
# ffmpeg: tts playback, libopus0: discord voice, libsndfile1: soundfile,
# libgl1 + libglib2.0-0: opencv, espeak-ng: kokoro phonemizer fallback
sudo apt-get update
sudo apt-get install -y ffmpeg libopus0 libsndfile1 libgl1 libglib2.0-0 espeak-ng curl ca-certificates
ok "system packages installed"

# 2. uv
step "checking for uv"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found, installing it"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV_BIN="$(command -v uv)"
ok "found $("$UV_BIN" --version) at $UV_BIN"

# 3. deps (uv sync also creates .venv and grabs python 3.13 if the system one doesn't match)
step "installing python dependencies"
if [ -f uv.lock ]; then
    "$UV_BIN" sync --locked || { warn "lockfile out of date, doing a normal sync"; "$UV_BIN" sync; }
else
    "$UV_BIN" sync
fi
ok "deps installed"

# 4. ollama
step "checking for ollama"
if ! command -v ollama >/dev/null 2>&1; then
    read -rp "   ollama isn't installed. install it now? [Y/n] " a
    if [[ -z "$a" || "$a" =~ ^[yY] ]]; then
        curl -fsSL https://ollama.com/install.sh | sh
    else
        warn "skipping ollama, /ask won't work until it's installed"
    fi
fi
if command -v ollama >/dev/null 2>&1; then
    sudo systemctl enable --now ollama >/dev/null 2>&1 || true
    ok "ollama running, pulling models (this can take a while the first time)"
    for m in llama3.1:8b gemma3:4b; do ollama pull "$m" || warn "failed to pull $m"; done
fi

# 5. sanity checks
step "sanity checks"
[ -f .env ] && ok ".env found" || warn "no .env file, the bot needs bot_token_dontleak=... in it"
for f in kokoro-v1.0.onnx voices-v1.0.bin; do
    [ -f "$f" ] && ok "$f found" || warn "missing $f, grab it from the kokoro-onnx github releases"
done

# 6. systemd service for 24/7
echo
read -rp "install meowbot as a systemd service (auto-start on boot, auto-restart on crash)? [Y/n] " a
if [[ -z "$a" || "$a" =~ ^[yY] ]]; then
    step "installing /etc/systemd/system/${SERVICE_NAME}.service"
    sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=MeowBot discord bot
Wants=network-online.target ollama.service
After=network-online.target ollama.service

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${UV_BIN} run --frozen python ${PROJECT_DIR}/${ENTRY}
Restart=always
RestartSec=10
# don't give up if it crash-loops (e.g. discord outage), just keep retrying
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE_NAME"
    ok "service installed and started"

    echo
    read -rp "auto pull new commits from git and restart the bot? [Y/n] " a
    if [[ -z "$a" || "$a" =~ ^[yY] ]]; then
        step "installing auto updater"
        ./scripts/install-autoupdate.sh
    fi
    echo
    printf '\033[36museful commands:\033[0m\n'
    echo "   sudo systemctl status $SERVICE_NAME     # is it running?"
    echo "   journalctl -u $SERVICE_NAME -f          # live logs"
    echo "   sudo systemctl restart $SERVICE_NAME    # restart after editing code"
    echo "   sudo systemctl stop $SERVICE_NAME       # stop it"
    echo "   sudo systemctl disable $SERVICE_NAME    # don't start on boot anymore"
else
    echo
    read -rp "run meowbot in the foreground now? [Y/n] " a
    if [[ -z "$a" || "$a" =~ ^[yY] ]]; then
        step "starting meowbot, ctrl+c to stop"
        exec "$UV_BIN" run python "$ENTRY"
    fi
    printf '\n\033[36maight, start it later with: uv run python %s\033[0m\n' "$ENTRY"
fi
