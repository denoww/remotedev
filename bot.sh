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

print_box() {
    # Desenha caixa com bordas alinhadas automaticamente
    # Uso: print_box "linha 1" "linha 2" ...
    local lines=("$@")
    local widths=()
    local max_w=0
    for line in "${lines[@]}"; do
        local w=$(printf '%s' "$line" | python3 -c "
import sys, unicodedata
s = sys.stdin.read()
print(sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s))
")
        widths+=("$w")
        [ "$w" -gt "$max_w" ] && max_w=$w
    done
    local border=$(printf '━%.0s' $(seq 1 $((max_w + 4))))
    echo "  ┏${border}┓"
    local i=0
    for line in "${lines[@]}"; do
        local w=${widths[$i]}
        local pad=""
        [ $((max_w - w)) -gt 0 ] && pad=$(printf ' %.0s' $(seq 1 $((max_w - w))))
        echo "  ┃  ${line}${pad}  ┃"
        i=$((i + 1))
    done
    echo "  ┗${border}┛"
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
    echo ""
    print_box "📦 INSTALAÇÃO DE NOVO BOT"
    echo ""

    # Verificar venv
    if [ ! -f "$BOT_DIR/venv/bin/python3" ]; then
        echo "  ❌ venv não encontrado. Rode antes:"
        echo "     python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
        exit 1
    fi

    # Verificar Claude Code
    if ! command -v claude &>/dev/null; then
        echo "  ❌ Claude Code não está instalado."
        echo "     Instale com: npm install -g @anthropic-ai/claude-code"
        exit 1
    fi

    if ! claude auth status &>/dev/null; then
        echo "  ❌ Claude Code não está logado."
        echo "     Faça login com: claude login"
        exit 1
    fi

    # ── Etapa 1: Workspace ──────────────────────────────────────────
    echo "  ── Etapa 1/5: Workspace ──"
    echo ""

    if [ "$(uname)" = "Darwin" ]; then
        DEFAULT_WORKSPACE="$HOME/Developer"
    else
        DEFAULT_WORKSPACE="$HOME/workspace"
    fi
    [ -n "${REMOTEDEV_WORKSPACE:-}" ] && DEFAULT_WORKSPACE="$REMOTEDEV_WORKSPACE"

    read -p "  Pasta dos projetos [$DEFAULT_WORKSPACE]: " WORKSPACE_INPUT
    WORKSPACE="${WORKSPACE_INPUT:-$DEFAULT_WORKSPACE}"

    if [ ! -d "$WORKSPACE" ]; then
        read -p "  Pasta '$WORKSPACE' não existe. Criar? (s/N): " criar_ws
        if [ "$criar_ws" = "s" ] || [ "$criar_ws" = "S" ]; then
            mkdir -p "$WORKSPACE"
            echo "  ✅ Pasta criada: $WORKSPACE"
        else
            echo "  ❌ Abortado."
            exit 1
        fi
    fi

    salvar_env_bashrc "REMOTEDEV_WORKSPACE" "$WORKSPACE"
    export "REMOTEDEV_WORKSPACE=$WORKSPACE"
    echo "  ✅ Workspace: $WORKSPACE"

    # ── Etapa 2: Nome do bot ────────────────────────────────────────
    echo ""
    echo "  ── Etapa 2/5: Nome do bot ──"
    echo ""

    local DEFAULT_BOT="botdev"
    read -p "  Nome do bot [$DEFAULT_BOT]: " BOT_NOME
    BOT_NOME=$(echo "${BOT_NOME:-$DEFAULT_BOT}" | tr '[:upper:]' '[:lower:]')

    while [ -f "$BOTS_DIR/$BOT_NOME.conf" ]; do
        echo "  ❌ Bot '$BOT_NOME' já está instalado."
        read -p "  Escolha outro nome (ou 'q' para sair): " BOT_NOME
        if [ "$BOT_NOME" = "q" ] || [ -z "$BOT_NOME" ]; then
            echo "  ❌ Abortado."
            exit 1
        fi
        BOT_NOME=$(echo "$BOT_NOME" | tr '[:upper:]' '[:lower:]')
    done

    echo "  ✅ Bot: $BOT_NOME"

    BOT_NOME_UPPER=$(echo "$BOT_NOME" | tr '[:lower:]' '[:upper:]')
    SERVICE_NAME="remotedev-$BOT_NOME"
    TOKEN_VAR="TELEGRAM_BOT_${BOT_NOME_UPPER}_TOKEN"
    CHAT_ID_VAR="TELEGRAM_${BOT_NOME_UPPER}_CHAT_ID"

    # ── Etapa 3: Token do Telegram ──────────────────────────────────
    echo ""
    echo "  ── Etapa 3/5: Token do Telegram ──"
    echo ""
    echo "  Bot novo?    Abra https://t.me/BotFather → /newbot"
    echo "  Bot existe?  Abra https://t.me/BotFather → /mybots → API Token"
    echo ""
    echo "  Copie o token e cole abaixo."
    echo ""
    read -p "  TOKEN: " BOT_TOKEN

    if [ -z "$BOT_TOKEN" ]; then
        echo "  ❌ Token não pode ser vazio."
        exit 1
    fi

    # Validar token com a API do Telegram
    TOKEN_RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe")
    TOKEN_OK=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok', False))" 2>/dev/null || echo "False")

    if [ "$TOKEN_OK" != "True" ]; then
        TOKEN_ERR=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('description', 'Resposta inválida'))" 2>/dev/null || echo "Não foi possível conectar à API do Telegram")
        echo "  ❌ Token inválido: $TOKEN_ERR"
        exit 1
    fi

    BOT_USERNAME=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['username'])" 2>/dev/null || echo "")

    # Salvar token no bashrc
    salvar_env_bashrc "$TOKEN_VAR" "$BOT_TOKEN"
    export "$TOKEN_VAR=$BOT_TOKEN"
    echo "  ✅ Token válido ($BOT_USERNAME)"

    # ── Etapa 4: Chat ID ───────────────────────────────────────────
    echo ""
    echo "  ── Etapa 4/5: Chat ID ──"
    echo ""
    if [ -n "$BOT_USERNAME" ]; then
        print_box "" "👉 ABRA O BOT NO TELEGRAM:" "" "   https://t.me/$BOT_USERNAME" "" "E mande qualquer mensagem." ""
    else
        echo "  Mande qualquer mensagem pro bot e aguarde..."
    fi
    echo ""
    echo "  ⏳ Aguardando mensagem..."
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
        echo "  ❌ Não consegui capturar o CHAT_ID. Tente novamente."
        exit 1
    fi

    echo "  ✅ CHAT_ID capturado: $BOT_CHAT_ID"

    # Salvar chat_id no bashrc
    salvar_env_bashrc "$CHAT_ID_VAR" "$BOT_CHAT_ID"
    export "$CHAT_ID_VAR=$BOT_CHAT_ID"

    # ── Etapa 5: Extras ────────────────────────────────────────────
    echo ""
    echo "  ── Etapa 5/5: Extras (opcionais) ──"

    # OpenAI API Key (opcional, para transcrição de áudio)
    echo ""
    echo "  🎤 OpenAI API Key (para transcrever áudios do Telegram)"
    echo "     Sem ela, áudios não serão interpretados."
    echo ""
    read -p "  Cole a OPENAI_API_KEY (ou Enter para pular): " OPENAI_KEY

    if [ -n "$OPENAI_KEY" ]; then
        salvar_env_bashrc "OPENAI_API_KEY" "$OPENAI_KEY"
        export "OPENAI_API_KEY=$OPENAI_KEY"
        echo "  ✅ OPENAI_API_KEY salvo"
    else
        OPENAI_KEY="${OPENAI_API_KEY:-}"
        if [ -n "$OPENAI_KEY" ]; then
            echo "  ℹ️  Usando OPENAI_API_KEY já configurada."
        else
            echo "  ⏭️  Pulado."
        fi
    fi

    # ffmpeg (para interpretar vídeos)
    echo ""
    read -p "  🎬 Instalar suporte a vídeos? Requer ffmpeg (s/N): " INSTALAR_FFMPEG
    if [ "$INSTALAR_FFMPEG" = "s" ] || [ "$INSTALAR_FFMPEG" = "S" ]; then
        if ! command -v ffmpeg &>/dev/null; then
            echo "  📦 Instalando ffmpeg..."
            if command -v apt-get &>/dev/null; then
                sudo apt-get install -y -qq ffmpeg
            elif command -v yum &>/dev/null; then
                sudo yum install -y -q ffmpeg
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y -q ffmpeg
            else
                echo "  ⚠️  Instale ffmpeg manualmente."
            fi
        fi
        if command -v ffmpeg &>/dev/null; then
            echo "  ✅ ffmpeg instalado"
        fi
    else
        echo "  ⏭️  Pulado."
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

    # Workspace e PATH para o systemd
    echo "REMOTEDEV_WORKSPACE=$WORKSPACE" >> "$SERVICE_DIR/$SERVICE_NAME.env"
    echo "PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" >> "$SERVICE_DIR/$SERVICE_NAME.env"

    if [ -n "$OPENAI_KEY" ]; then
        echo "OPENAI_API_KEY=$OPENAI_KEY" >> "$SERVICE_DIR/$SERVICE_NAME.env"
    fi

    # Criar serviço de notificação de falha
    cat > "$SERVICE_DIR/remotedev-notify@.service" << EOF
[Unit]
Description=Notifica falha de %i no Telegram

[Service]
Type=oneshot
ExecStart=$BOT_DIR/notify-failure.sh %i
EOF

    # Criar serviço
    cat > "$SERVICE_DIR/$SERVICE_NAME.service" << EOF
[Unit]
Description=remotedev [$BOT_NOME] - Telegram Bot
After=network-online.target
Wants=network-online.target
OnFailure=remotedev-notify@%n.service

[Service]
Type=simple
WorkingDirectory=$BOT_DIR
ExecStartPre=-/usr/bin/git -C $BOT_DIR pull --ff-only
ExecStart=$BOT_DIR/venv/bin/python3 remotedev.py $BOT_NOME
Restart=always
RestartSec=5
EnvironmentFile=$SERVICE_DIR/$SERVICE_NAME.env

[Install]
WantedBy=default.target
EOF

    # Ativar e iniciar
    echo ""
    echo "  ⚙️  Configurando serviço..."
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"
    loginctl enable-linger "$USER"

    # ── Resultado ───────────────────────────────────────────────────
    echo ""
    print_box "✅ Bot [$BOT_NOME] instalado e rodando!"
    echo ""
    echo "  Abra o Telegram e envie /start pro bot."
    echo "  Comandos são registrados automaticamente ao iniciar."
    echo ""
    echo "  ── Gerenciar ──────────────────────────────────────"
    echo "  Logs:          ./bot.sh logs $BOT_NOME"
    echo "  Logs Claude:   ./bot.sh logs-claude $BOT_NOME"
    echo "  Reiniciar:     ./bot.sh restart $BOT_NOME"
    echo "  Status:        ./bot.sh status"
    echo "  Desinstalar:   ./bot.sh uninstall"
    echo ""
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
    local poll_script="$BOT_DIR/gitpull-and-restart.sh"
    local poll_log="$BOT_DIR/gitpull.log"
    case "$action" in
        on)
            (crontab -l 2>/dev/null | grep -v gitpull-and-restart; echo "*/2 * * * * $poll_script") | crontab -
            echo "✅ Polling ativado (a cada 2 min)"
            ;;
        off)
            crontab -l 2>/dev/null | grep -v gitpull-and-restart | crontab -
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
            if crontab -l 2>/dev/null | grep -q gitpull-and-restart; then
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

cmd_setup_ec2() {
    echo "══════════════════════════════════════════"
    echo "🖥️  Setup remotedev para EC2/servidor"
    echo "══════════════════════════════════════════"
    echo ""

    # Detectar OS
    if command -v apt-get &>/dev/null; then
        PKG_MANAGER="apt-get"
        echo "📦 Instalando dependências (apt)..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3 python3-venv git ffmpeg
    elif command -v yum &>/dev/null; then
        PKG_MANAGER="yum"
        echo "📦 Instalando dependências (yum)..."
        sudo yum install -y -q python3 git ffmpeg
    elif command -v dnf &>/dev/null; then
        PKG_MANAGER="dnf"
        echo "📦 Instalando dependências (dnf)..."
        sudo dnf install -y -q python3 git ffmpeg
    else
        echo "⚠️  Gerenciador de pacotes não detectado. Instale manualmente: python3, python3-venv, git, ffmpeg"
    fi

    # Verificar Python
    if ! command -v python3 &>/dev/null; then
        echo "❌ python3 não encontrado. Instale e tente novamente."
        exit 1
    fi
    echo "✅ Python: $(python3 --version)"

    # Verificar git
    if ! command -v git &>/dev/null; then
        echo "❌ git não encontrado. Instale e tente novamente."
        exit 1
    fi
    echo "✅ Git: $(git --version)"

    # Criar venv se não existir
    if [ ! -d "$BOT_DIR/venv" ]; then
        echo ""
        echo "🐍 Criando virtualenv..."
        python3 -m venv "$BOT_DIR/venv"
    fi

    echo "📦 Instalando dependências Python..."
    "$BOT_DIR/venv/bin/python3" -m pip install -q -r "$BOT_DIR/requirements.txt"
    echo "✅ Dependências instaladas"

    # Instalar Claude CLI
    if ! command -v claude &>/dev/null; then
        echo ""
        echo "🧠 Instalando Claude CLI..."
        if command -v npm &>/dev/null; then
            npm install -g @anthropic-ai/claude-code
            echo "✅ Claude CLI instalado"
        else
            echo "⚠️  npm não encontrado. Instale Node.js e rode:"
            echo "   npm install -g @anthropic-ai/claude-code"
        fi
    else
        echo "✅ Claude CLI: $(claude --version 2>/dev/null || echo 'instalado')"
    fi

    # Criar workspace se não existir
    if [ "$(uname)" = "Darwin" ]; then
        DEFAULT_WORKSPACE="$HOME/Developer"
    else
        DEFAULT_WORKSPACE="$HOME/workspace"
    fi
    [ -n "${REMOTEDEV_WORKSPACE:-}" ] && DEFAULT_WORKSPACE="$REMOTEDEV_WORKSPACE"

    read -p "Pasta dos projetos [$DEFAULT_WORKSPACE]: " WORKSPACE_INPUT
    SETUP_WORKSPACE="${WORKSPACE_INPUT:-$DEFAULT_WORKSPACE}"
    mkdir -p "$SETUP_WORKSPACE"
    salvar_env_bashrc "REMOTEDEV_WORKSPACE" "$SETUP_WORKSPACE"
    echo "✅ Workspace: $SETUP_WORKSPACE"

    # Habilitar systemd para user
    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$USER" 2>/dev/null || true
    fi
    mkdir -p "$SERVICE_DIR"

    echo ""
    echo "══════════════════════════════════════════"
    echo "✅ Setup concluído!"
    echo ""
    echo "Próximos passos:"
    echo "  1. Clone seus projetos em $SETUP_WORKSPACE/"
    echo "  2. Rode: ./bot.sh install"
    echo "══════════════════════════════════════════"
}

cmd_help() {
    echo "remotedev — Script centralizado"
    echo ""
    echo "Uso: ./bot.sh <comando> [argumentos]"
    echo ""
    echo "Comandos:"
    echo "  setup_ec2                      Setup inicial para EC2/servidor"
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
    setup_ec2)  cmd_setup_ec2 ;;
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
