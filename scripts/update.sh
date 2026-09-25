#!/usr/bin/env bash
# meowbot auto updater
# checks for new commits, pulls them (fast-forward only, never touches .env or other untracked/ignored files),
# re-syncs deps if they changed, makes sure the code at least compiles, then restarts the bot.
# the meowbot-update.timer runs this every few minutes, but you can also run it by hand.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."
PROJECT_DIR="$(pwd)"
SERVICE_NAME="meowbot"
ENTRY="main.py"

# git/uv stuff always runs as whoever owns the project folder, so root never ends up owning files in here
OWNER="$(stat -c %U "$PROJECT_DIR")"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
as_owner() {
    if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
        runuser -u "$OWNER" -- env HOME="$OWNER_HOME" PATH="$OWNER_HOME/.local/bin:$PATH" "$@"
    else
        env PATH="$HOME/.local/bin:$PATH" "$@"
    fi
}
restart_bot() {
    if [ "$(id -u)" -eq 0 ]; then systemctl restart "$SERVICE_NAME"; else sudo systemctl restart "$SERVICE_NAME"; fi
}
log() { echo "[updater] $*"; }

# don't let two updates run at the same time
exec 9>"/tmp/${SERVICE_NAME}-update.lock"
flock -n 9 || { log "another update is already running, skipping"; exit 0; }

UPSTREAM="$(as_owner git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -z "$UPSTREAM" ]; then
    log "current branch has no upstream (no remote set up?), nothing to pull"
    exit 0
fi

as_owner git fetch --quiet "${UPSTREAM%%/*}"

OLD="$(as_owner git rev-parse HEAD)"
NEW="$(as_owner git rev-parse "$UPSTREAM")"
if [ "$OLD" = "$NEW" ]; then
    exit 0   # already up to date, stay quiet
fi

if ! as_owner git merge-base --is-ancestor HEAD "$UPSTREAM"; then
    log "local branch has commits that aren't on $UPSTREAM (diverged), not touching it. sort it out by hand"
    exit 1
fi

log "update found: ${OLD:0:7} -> ${NEW:0:7}"
as_owner git log --oneline "$OLD..$NEW" | sed 's/^/[updater]   /'

# --ff-only never makes merge commits, and git refuses (without changing anything) if the update would
# overwrite local edits or an untracked file. ignored files like .env and the model files are never touched
if ! as_owner git merge --ff-only --quiet "$UPSTREAM"; then
    log "pull failed (probably local edits in the way), bot left alone on ${OLD:0:7}"
    exit 1
fi

rollback() {
    log "$1, rolling back to ${OLD:0:7}"
    as_owner git reset --keep --quiet "$OLD"
    as_owner uv sync --frozen --quiet || true
    exit 1
}

if as_owner git diff --quiet "$OLD" "$NEW" -- pyproject.toml uv.lock; then
    log "deps unchanged"
else
    log "deps changed, syncing"
    as_owner uv sync --frozen --quiet || rollback "uv sync failed"
fi

# catch syntax errors before we restart into a broken bot
as_owner uv run --frozen python -m py_compile "$ENTRY" || rollback "new code doesn't compile"

log "restarting $SERVICE_NAME"
restart_bot
log "updated to ${NEW:0:7} :3"
