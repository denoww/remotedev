#!/bin/bash
#
# poll-and-restart.sh — Polling de commits novos no remote
#
# Roda via cron a cada 2 min. Se detectar commits novos em origin/main,
# faz pull e reinicia todos os bots instalados.
#

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BOTS_DIR="$REPO_DIR/bots"
BRANCH="main"

# Fetch silencioso
git -C "$REPO_DIR" fetch origin "$BRANCH" --quiet 2>/dev/null || exit 0

LOCAL=$(git -C "$REPO_DIR" rev-parse "$BRANCH" 2>/dev/null)
REMOTE=$(git -C "$REPO_DIR" rev-parse "origin/$BRANCH" 2>/dev/null)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

# Tem commits novos — pull e reinicia todos os bots
echo "$(date '+%Y-%m-%d %H:%M:%S') — Novos commits detectados (local=$LOCAL remote=$REMOTE)" >> "$REPO_DIR/poll.log"

git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" >> "$REPO_DIR/poll.log" 2>&1

for conf in "$BOTS_DIR"/*.conf; do
    [ -f "$conf" ] || continue
    nome=$(basename "$conf" .conf)
    systemctl --user restart "remotedev-$nome" 2>/dev/null && \
        echo "$(date '+%Y-%m-%d %H:%M:%S') — 🔄 Bot [$nome] reiniciado" >> "$REPO_DIR/poll.log"
done
