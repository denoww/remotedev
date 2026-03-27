#!/bin/bash
#
# Remove uma instância do RodrigoDevBot do systemd.
#
# Uso:
#   ./uninstall_service.sh dev
#   ./uninstall_service.sh prod
#

if [ -z "$1" ]; then
    echo "Uso: ./uninstall_service.sh <nome_bot>"
    echo "  Ex: ./uninstall_service.sh dev"
    exit 1
fi

BOT_NOME="$1"
SERVICE_NAME="rodrigodevbot-$BOT_NOME"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "🗑️  Removendo serviço $SERVICE_NAME..."

systemctl --user stop "$SERVICE_NAME" 2>/dev/null
systemctl --user disable "$SERVICE_NAME" 2>/dev/null
rm -f "$SERVICE_DIR/$SERVICE_NAME.service"
rm -f "$SERVICE_DIR/$SERVICE_NAME.env"
systemctl --user daemon-reload

echo "✅ Serviço [$BOT_NOME] removido."
