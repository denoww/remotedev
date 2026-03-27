#!/bin/bash
# Mostra os logs das execuções do Claude Code em tempo real
#
# Uso:
#   ./logs_do_claude.sh          — todos os projetos
#   ./logs_do_claude.sh scsip    — filtra por projeto
#

LOG_FILE="$HOME/workspace/RodrigoDevBot/claude.log"

if [ ! -f "$LOG_FILE" ]; then
    echo "Aguardando primeiras execuções do /claude..."
    touch "$LOG_FILE"
fi

if [ -n "$1" ]; then
    tail -f "$LOG_FILE" | grep --line-buffered -i "$1"
else
    tail -f "$LOG_FILE"
fi
