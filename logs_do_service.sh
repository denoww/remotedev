#!/bin/bash
# Mostra os logs do serviço do bot em tempo real.
#
# Uso:
#   ./logs_do_service.sh dev
#   ./logs_do_service.sh prod
#

if [ -z "$1" ]; then
    echo "Uso: ./logs_do_service.sh <nome_bot>"
    echo "  Ex: ./logs_do_service.sh dev"
    exit 1
fi

journalctl --user -u "rodrigodevbot-$1" -f
