#!/bin/bash
#
# gitpull-and-restart.sh — Polling de commits novos no remote
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
echo "$(date '+%Y-%m-%d %H:%M:%S') — Novos commits detectados (local=$LOCAL remote=$REMOTE)" >> "$REPO_DIR/gitpull.log"

# Resumo dos commits novos
COMMITS=$(git -C "$REPO_DIR" log --oneline "$LOCAL..$REMOTE" 2>/dev/null | head -10)

git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" >> "$REPO_DIR/gitpull.log" 2>&1

SERVICE_DIR="$HOME/.config/systemd/user"

for conf in "$BOTS_DIR"/*.conf; do
    [ -f "$conf" ] || continue
    nome=$(basename "$conf" .conf)
    nome_upper=$(echo "$nome" | tr '[:lower:]' '[:upper:]')
    env_file="$SERVICE_DIR/remotedev-$nome.env"

    # Notificar no Telegram antes de reiniciar
    if [ -f "$env_file" ]; then
        token=$(grep "^TELEGRAM_BOT_${nome_upper}_TOKEN=" "$env_file" | cut -d= -f2-)
        chat_id=$(grep "^TELEGRAM_${nome_upper}_CHAT_ID=" "$env_file" | cut -d= -f2-)
        if [ -n "$token" ] && [ -n "$chat_id" ]; then
            curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" \
                -d chat_id="$chat_id" \
                -d parse_mode="HTML" \
                -d text="🔄 <b>Atualização detectada!</b>

Reiniciando bot em 5s...

<code>$COMMITS</code>" > /dev/null 2>&1
        fi
    fi

    sleep 5
    systemctl --user restart "remotedev-$nome" 2>/dev/null && \
        echo "$(date '+%Y-%m-%d %H:%M:%S') — 🔄 Bot [$nome] reiniciado" >> "$REPO_DIR/gitpull.log"
done
