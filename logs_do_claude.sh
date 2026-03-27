#!/bin/bash
# Mostra os logs das execuções do Claude Code em tempo real.
#
# Uso:
#   ./logs_do_claude.sh dev              — todos os projetos do bot dev
#   ./logs_do_claude.sh dev scsip        — filtra por projeto
#

if [ -z "$1" ]; then
    echo "Uso: ./logs_do_claude.sh <nome_bot> [filtro]"
    echo "  Ex: ./logs_do_claude.sh dev"
    echo "  Ex: ./logs_do_claude.sh dev scsip"
    exit 1
fi

BOT_NOME="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/claude-$BOT_NOME.log"

if [ ! -f "$LOG_FILE" ]; then
    echo "Aguardando primeiras execuções do /claude no bot [$BOT_NOME]..."
    touch "$LOG_FILE"
fi

if [ -n "$2" ]; then
    tail -f "$LOG_FILE" | grep --line-buffered -i "$2"
else
    tail -f "$LOG_FILE"
fi
