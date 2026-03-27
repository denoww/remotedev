#!/bin/bash
#
# Instala uma instância do RodrigoDevBot como serviço systemd.
#
# Uso:
#   ./install_service.sh dev
#   ./install_service.sh prod
#

set -e

if [ -z "$1" ]; then
    echo "Uso: ./install_service.sh <nome_bot>"
    echo "  Ex: ./install_service.sh dev"
    echo "  Ex: ./install_service.sh prod"
    exit 1
fi

BOT_NOME="$1"
BOT_NOME_UPPER=$(echo "$BOT_NOME" | tr '[:lower:]' '[:upper:]')
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="rodrigodevbot-$BOT_NOME"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"
ENV_FILE="$SERVICE_DIR/$SERVICE_NAME.env"

TOKEN_VAR="TELEGRAM_BOT_${BOT_NOME_UPPER}_TOKEN"
CHAT_ID_VAR="TELEGRAM_${BOT_NOME_UPPER}_CHAT_ID"

echo "📦 Instalando bot [$BOT_NOME] como serviço..."
echo "   Diretório: $BOT_DIR"
echo "   Serviço:   $SERVICE_NAME"
echo ""

# Verificar venv
if [ ! -f "$BOT_DIR/venv/bin/python3" ]; then
    echo "❌ venv não encontrado. Rode antes:"
    echo "   python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Verificar variáveis de ambiente
TOKEN_VAL="${!TOKEN_VAR}"
CHAT_ID_VAL="${!CHAT_ID_VAR}"

if [ -z "$TOKEN_VAL" ]; then
    echo "❌ $TOKEN_VAR não definido. Veja o README."
    exit 1
fi

if [ -z "$CHAT_ID_VAL" ] || [ "$CHAT_ID_VAL" = "0" ]; then
    echo "❌ $CHAT_ID_VAR não definido. Veja o README."
    exit 1
fi

# Criar diretório do systemd
mkdir -p "$SERVICE_DIR"

# Criar arquivo de variáveis
cat > "$ENV_FILE" << EOF
$TOKEN_VAR=$TOKEN_VAL
$CHAT_ID_VAR=$CHAT_ID_VAL
EOF
echo "✅ Variáveis salvas em $ENV_FILE"

# Criar serviço
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=RodrigoDevBot [$BOT_NOME] - Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python3 telegram_desktop_bot.py $BOT_NOME
Restart=always
RestartSec=5
EnvironmentFile=$ENV_FILE

[Install]
WantedBy=default.target
EOF
echo "✅ Serviço criado em $SERVICE_FILE"

# Ativar e iniciar
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"
loginctl enable-linger "$USER"

echo ""
echo "✅ Bot [$BOT_NOME] instalado e rodando!"
echo ""
echo "   Ver status:  systemctl --user status $SERVICE_NAME"
echo "   Ver logs:    ./logs_do_service.sh $BOT_NOME"
echo "   Reiniciar:   systemctl --user restart $SERVICE_NAME"
echo "   Parar:       systemctl --user stop $SERVICE_NAME"
echo "   Remover:     ./uninstall_service.sh $BOT_NOME"
