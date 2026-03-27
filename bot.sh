#!/bin/bash
#
# remotedev — Script centralizado
#
# Uso:
#   ./bot.sh install                        — instala um novo bot (interativo)
#   ./bot.sh uninstall                      — lista bots e remove o escolhido
#   ./bot.sh list                           — lista bots instalados
#   ./bot.sh status                         — status de todos os bots
#   ./bot.sh restart                        — pergunta qual bot reiniciar
#   ./bot.sh restart dev_desktop            — reinicia direto
#   ./bot.sh stop dev_desktop               — para o bot
#   ./bot.sh start dev_desktop              — inicia o bot
#   ./bot.sh logs dev_desktop               — logs do serviço
#   ./bot.sh logs-claude dev_desktop        — logs do Claude
#   ./bot.sh logs-claude dev_desktop scsip  — filtra por projeto
#

set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOTS_DIR="$BOT_DIR/bots"
SERVICE_DIR="$HOME/.config/systemd/user"

mkdir -p "$BOTS_DIR"

# ── Helpers ──────────────────────────────────────────────────────────

listar_bots() {
    if [ -z "$(ls "$BOTS_DIR"/*.conf 2>/dev/null)" ]; then
        echo "Nenhum bot instalado."
        return 1
    fi
    for conf in "$BOTS_DIR"/*.conf; do
        nome=$(basename "$conf" .conf)
        source "$conf"
        status=$(systemctl --user is-active "remotedev-$nome" 2>/dev/null || true)
        if [ "$status" = "active" ]; then
            echo "  🟢 $nome"
        else
            echo "  🔴 $nome"
        fi
    done
}

escolher_bot() {
    local acao="$1"
    local bots=()
    for conf in "$BOTS_DIR"/*.conf; do
        [ -f "$conf" ] || continue
        bots+=($(basename "$conf" .conf))
    done

    if [ ${#bots[@]} -eq 0 ]; then
        echo "Nenhum bot instalado." >&2
        exit 1
    fi

    echo "Bots instalados:" >&2
    for i in "${!bots[@]}"; do
        nome="${bots[$i]}"
        status=$(systemctl --user is-active "remotedev-$nome" 2>/dev/null || true)
        if [ "$status" = "active" ]; then
            echo "  $((i+1))) 🟢 $nome" >&2
        else
            echo "  $((i+1))) 🔴 $nome" >&2
        fi
    done

    echo "" >&2
    read -p "Qual bot deseja $acao? (número): " escolha
    idx=$((escolha - 1))

    if [ $idx -lt 0 ] || [ $idx -ge ${#bots[@]} ]; then
        echo "❌ Opção inválida." >&2
        exit 1
    fi

    echo "${bots[$idx]}"
}

salvar_env_bashrc() {
    local var_name="$1"
    local var_value="$2"
    grep -q "$var_name" ~/.bashrc && \
        sed -i "s|export $var_name=.*|export $var_name=\"$var_value\"|" ~/.bashrc || \
        echo "export $var_name=\"$var_value\"" >> ~/.bashrc
}

# ── Comandos ─────────────────────────────────────────────────────────

cmd_install() {
    echo "📦 Instalação de novo bot"
    echo ""

    # Verificar venv
    if [ ! -f "$BOT_DIR/venv/bin/python3" ]; then
        echo "❌ venv não encontrado. Rode antes:"
        echo "   python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
        exit 1
    fi

    # Nome do bot
    read -p "Nome do bot (ex: botdev, botanalise, botlimpeza, botcontrolarpc): " BOT_NOME
    BOT_NOME=$(echo "$BOT_NOME" | tr '[:upper:]' '[:lower:]')

    if [ -z "$BOT_NOME" ]; then
        echo "❌ Nome não pode ser vazio."
        exit 1
    fi

    BOT_NOME_UPPER=$(echo "$BOT_NOME" | tr '[:lower:]' '[:upper:]')
    SERVICE_NAME="remotedev-$BOT_NOME"
    TOKEN_VAR="TELEGRAM_BOT_${BOT_NOME_UPPER}_TOKEN"
    CHAT_ID_VAR="TELEGRAM_${BOT_NOME_UPPER}_CHAT_ID"

    # Token
    echo ""
    echo "Abra o Telegram → @BotFather → /newbot → copie o token"
    read -p "Cole o TOKEN: " BOT_TOKEN

    if [ -z "$BOT_TOKEN" ]; then
        echo "❌ Token não pode ser vazio."
        exit 1
    fi

    # Salvar token no bashrc
    salvar_env_bashrc "$TOKEN_VAR" "$BOT_TOKEN"
    export "$TOKEN_VAR=$BOT_TOKEN"
    echo "✅ $TOKEN_VAR salvo no ~/.bashrc"

    # Chat ID
    echo ""
    echo "Agora vamos descobrir seu CHAT_ID."
    echo "Mande qualquer mensagem pro bot no Telegram e aguarde..."
    echo ""

    BOT_CHAT_ID=$("$BOT_DIR/venv/bin/python3" -c "
import os, sys
os.environ['${TOKEN_VAR}'] = '${BOT_TOKEN}'
sys.argv = ['', '${BOT_NOME}']

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

async def get_id(update, context):
    print(update.effective_chat.id)
    import signal; os.kill(os.getpid(), signal.SIGINT)

app = Application.builder().token('${BOT_TOKEN}').build()
app.add_handler(MessageHandler(filters.ALL, get_id))
try:
    app.run_polling()
except (KeyboardInterrupt, SystemExit):
    pass
" 2>/dev/null | tail -1)

    if [ -z "$BOT_CHAT_ID" ]; then
        echo "❌ Não consegui capturar o CHAT_ID. Tente novamente."
        exit 1
    fi

    echo ""
    echo "✅ CHAT_ID capturado: $BOT_CHAT_ID"

    # Salvar chat_id no bashrc
    salvar_env_bashrc "$CHAT_ID_VAR" "$BOT_CHAT_ID"
    export "$CHAT_ID_VAR=$BOT_CHAT_ID"
    echo "✅ $CHAT_ID_VAR salvo no ~/.bashrc"

    # OpenAI API Key (opcional, para transcrição de áudio)
    echo ""
    echo "🎤 OpenAI API Key (para transcrever áudios do Telegram)"
    echo "   Opcional — sem ela, áudios não serão interpretados."
    read -p "Cole a OPENAI_API_KEY (ou Enter para pular): " OPENAI_KEY

    if [ -n "$OPENAI_KEY" ]; then
        salvar_env_bashrc "OPENAI_API_KEY" "$OPENAI_KEY"
        export "OPENAI_API_KEY=$OPENAI_KEY"
        echo "✅ OPENAI_API_KEY salvo no ~/.bashrc"
    else
        OPENAI_KEY="${OPENAI_API_KEY:-}"
        if [ -n "$OPENAI_KEY" ]; then
            echo "ℹ️  Usando OPENAI_API_KEY já configurada."
        else
            echo "⏭️  Pulado. Áudios não serão transcritos."
        fi
    fi

    # Salvar config local
    cat > "$BOTS_DIR/$BOT_NOME.conf" << EOF
BOT_TOKEN="$BOT_TOKEN"
BOT_CHAT_ID="$BOT_CHAT_ID"
TOKEN_VAR="$TOKEN_VAR"
CHAT_ID_VAR="$CHAT_ID_VAR"
EOF

    # Criar env do systemd
    mkdir -p "$SERVICE_DIR"
    cat > "$SERVICE_DIR/$SERVICE_NAME.env" << EOF
$TOKEN_VAR=$BOT_TOKEN
$CHAT_ID_VAR=$BOT_CHAT_ID
EOF

    if [ -n "$OPENAI_KEY" ]; then
        echo "OPENAI_API_KEY=$OPENAI_KEY" >> "$SERVICE_DIR/$SERVICE_NAME.env"
    fi

    # Criar serviço
    cat > "$SERVICE_DIR/$SERVICE_NAME.service" << EOF
[Unit]
Description=remotedev [$BOT_NOME] - Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStartPre=/usr/bin/git -C $BOT_DIR pull --ff-only
ExecStart=$BOT_DIR/venv/bin/python3 remotedev.py $BOT_NOME
Restart=always
RestartSec=5
EnvironmentFile=$SERVICE_DIR/$SERVICE_NAME.env

[Install]
WantedBy=default.target
EOF

    # Ativar e iniciar
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    loginctl enable-linger "$USER"

    echo ""
    echo "═══════════════════════════════════════════"
    echo "✅ Bot [$BOT_NOME] instalado e rodando!"
    echo ""
    echo "   Abra o Telegram e envie /start pro bot."
    echo ""
    BOT_USERNAME=$(curl -s "https://api.telegram.org/bot${!TOKEN_VAR}/getMe" | python3 -c "import sys,json; print('@'+json.load(sys.stdin)['result']['username'])" 2>/dev/null || echo "seu bot")
    echo "── Registrar comandos no Telegram ──"
    echo ""
    echo "   1. Abra @BotFather no Telegram"
    echo ""
    echo "   2. Envie:"
    echo "      /setcommands"
    echo ""
    echo "   3. Selecione:"
    echo "      $BOT_USERNAME"
    echo ""
    echo "   4. Cole tudo abaixo:"
    echo "   ─────────────────────────────────"
    echo "start - Menu de ajuda"
    echo "help - Menu de ajuda"
    echo "new - Nova sessao Claude (limpa contexto)"
    echo "p - Trocar projeto"
    echo "bash - Executar comando no terminal"
    echo "git - Comandos git"
    echo "gitpush - Add, commit e push automatico"
    echo "ping_pc - Verifica se desktop esta online"
    echo "meu_chat_id - Mostra seu chat_id"
    echo "restart_bot - Reinicia o bot"
    echo "   ─────────────────────────────────"
    echo ""
    echo "── Gerenciar ──"
    echo "   Logs:          ./bot.sh logs $BOT_NOME"
    echo "   Logs Claude:   ./bot.sh logs-claude $BOT_NOME"
    echo "   Reiniciar:     ./bot.sh restart $BOT_NOME"
    echo "   Desinstalar:   ./bot.sh uninstall"
    echo "═══════════════════════════════════════════"
}

cmd_uninstall() {
    resultado=$(escolher_bot "desinstalar")
    BOT_NOME=$(echo "$resultado" | tail -1)
    SERVICE_NAME="remotedev-$BOT_NOME"

    echo ""
    read -p "Tem certeza que deseja remover o bot [$BOT_NOME]? (s/N): " confirma
    if [ "$confirma" != "s" ] && [ "$confirma" != "S" ]; then
        echo "Cancelado."
        exit 0
    fi

    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_DIR/$SERVICE_NAME.service"
    rm -f "$SERVICE_DIR/$SERVICE_NAME.env"
    rm -f "$BOTS_DIR/$BOT_NOME.conf"
    systemctl --user daemon-reload

    echo "✅ Bot [$BOT_NOME] removido."
}

cmd_list() {
    echo "Bots instalados:"
    listar_bots
}

cmd_status() {
    echo "Status dos bots:"
    echo ""
    for conf in "$BOTS_DIR"/*.conf; do
        [ -f "$conf" ] || { echo "Nenhum bot instalado."; exit 0; }
        nome=$(basename "$conf" .conf)
        echo "── $nome ──"
        systemctl --user status "remotedev-$nome" --no-pager 2>&1 | head -5
        echo ""
    done
}

cmd_logs() {
    local nome="$1"
    if [ -z "$nome" ]; then
        resultado=$(escolher_bot "ver logs")
        nome=$(echo "$resultado" | tail -1)
    fi
    journalctl --user -u "remotedev-$nome" -f
}

cmd_logs_claude() {
    local nome="$1"
    local filtro="$2"
    if [ -z "$nome" ]; then
        resultado=$(escolher_bot "ver logs do Claude")
        nome=$(echo "$resultado" | tail -1)
    fi

    local log_file="$BOT_DIR/claude-$nome.log"
    if [ ! -f "$log_file" ]; then
        echo "Aguardando primeiras execuções do /c no bot [$nome]..."
        touch "$log_file"
    fi

    if [ -n "$filtro" ]; then
        tail -f "$log_file" | grep --line-buffered -i "$filtro"
    else
        tail -f "$log_file"
    fi
}

cmd_restart() {
    local nome="$1"
    if [ -z "$nome" ]; then
        resultado=$(escolher_bot "reiniciar")
        nome=$(echo "$resultado" | tail -1)
    fi
    systemctl --user restart "remotedev-$nome"
    echo "✅ Bot [$nome] reiniciado."
}

cmd_stop() {
    local nome="$1"
    if [ -z "$nome" ]; then
        resultado=$(escolher_bot "parar")
        nome=$(echo "$resultado" | tail -1)
    fi
    systemctl --user stop "remotedev-$nome"
    echo "🔴 Bot [$nome] parado."
}

cmd_start() {
    local nome="$1"
    if [ -z "$nome" ]; then
        resultado=$(escolher_bot "iniciar")
        nome=$(echo "$resultado" | tail -1)
    fi
    systemctl --user start "remotedev-$nome"
    echo "🟢 Bot [$nome] iniciado."
}

cmd_poll() {
    local action="${1:-status}"
    local poll_script="$BOT_DIR/poll-and-restart.sh"
    local poll_log="$BOT_DIR/poll.log"
    case "$action" in
        on)
            (crontab -l 2>/dev/null | grep -v poll-and-restart; echo "*/2 * * * * $poll_script") | crontab -
            echo "✅ Polling ativado (a cada 2 min)"
            ;;
        off)
            crontab -l 2>/dev/null | grep -v poll-and-restart | crontab -
            echo "🔴 Polling desativado"
            ;;
        log)
            if [ -f "$poll_log" ]; then
                tail -20 "$poll_log"
            else
                echo "Nenhum log de polling ainda."
            fi
            ;;
        status|*)
            if crontab -l 2>/dev/null | grep -q poll-and-restart; then
                echo "🟢 Polling ativo (a cada 2 min)"
            else
                echo "🔴 Polling inativo"
            fi
            if [ -f "$poll_log" ]; then
                echo ""
                echo "Últimas entradas:"
                tail -5 "$poll_log"
            fi
            ;;
    esac
}

cmd_help() {
    echo "remotedev — Script centralizado"
    echo ""
    echo "Uso: ./bot.sh <comando> [argumentos]"
    echo ""
    echo "Comandos:"
    echo "  install                        Instala um novo bot (interativo)"
    echo "  uninstall                      Lista bots e remove o escolhido"
    echo "  list                           Lista bots instalados"
    echo "  status                         Status de todos os bots"
    echo "  restart [nome]                 Reinicia um bot"
    echo "  stop [nome]                    Para um bot"
    echo "  start [nome]                   Inicia um bot"
    echo "  logs [nome]                    Logs do serviço"
    echo "  logs-claude [nome] [filtro]    Logs do Claude"
    echo "  poll [on|off|log|status]       Gerencia polling de commits"
}

# ── Main ─────────────────────────────────────────────────────────────

case "${1:-}" in
    install)    cmd_install ;;
    uninstall)  cmd_uninstall ;;
    list)       cmd_list ;;
    status)     cmd_status ;;
    logs)       cmd_logs "$2" ;;
    logs-claude) cmd_logs_claude "$2" "$3" ;;
    restart)    cmd_restart "$2" ;;
    stop)       cmd_stop "$2" ;;
    start)      cmd_start "$2" ;;
    poll)       cmd_poll "$2" ;;
    *)          cmd_help ;;
esac
