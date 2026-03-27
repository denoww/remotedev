#!/bin/bash
#
# Envia notificação no Telegram quando um serviço remotedev falha.
# Chamado pelo systemd via OnFailure=remotedev-notify@%n.service
#
# Argumento: nome completo do serviço (ex: remotedev-botdev.service)
#

SERVICE_NAME="${1%.service}"  # remove .service
BOT_NOME="${SERVICE_NAME#remotedev-}"  # remove prefixo remotedev-

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HOME/.config/systemd/user/$SERVICE_NAME.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "Arquivo env não encontrado: $ENV_FILE"
    exit 1
fi

BOT_NOME_UPPER=$(echo "$BOT_NOME" | tr '[:lower:]' '[:upper:]')
TOKEN=$(grep "^TELEGRAM_BOT_${BOT_NOME_UPPER}_TOKEN=" "$ENV_FILE" | cut -d= -f2-)
CHAT_ID=$(grep "^TELEGRAM_${BOT_NOME_UPPER}_CHAT_ID=" "$ENV_FILE" | cut -d= -f2-)

if [ -z "$TOKEN" ] || [ -z "$CHAT_ID" ]; then
    echo "Token ou chat_id não encontrado no env."
    exit 1
fi

HOSTNAME=$(hostname)
TIMESTAMP=$(date '+%d/%m %H:%M')

curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d chat_id="$CHAT_ID" \
    -d parse_mode="HTML" \
    -d text="⚠️ <b>Serviço caiu!</b>

<code>$SERVICE_NAME</code> falhou em <b>$HOSTNAME</b>
$TIMESTAMP

O systemd desistiu de reiniciar. Para reiniciar manualmente:
<code>/restart_bot</code>" > /dev/null
