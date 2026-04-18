import os
import sys

def carregar_config():
    """Carrega config baseado no nome do bot passado como argumento."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Uso: python3 remotedev.py <nome_bot>")
        print("  Ex: python3 remotedev.py dev")
        print("  Ex: python3 remotedev.py prod")
        sys.exit(1)

    nome = args[0].lower()
    nome_upper = nome.upper()

    token = os.environ.get(f"TELEGRAM_BOT_{nome_upper}_TOKEN", "")
    owner_id = int(os.environ.get(f"TELEGRAM_{nome_upper}_CHAT_ID", "0"))

    return nome, token, owner_id

BOT_NOME, TOKEN, OWNER_CHAT_ID = carregar_config()
BOT_SERVICE = f"remotedev-{BOT_NOME}"
BOT_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKSPACE = os.environ.get("REMOTEDEV_WORKSPACE", os.path.expanduser("~/workspace"))

def descobrir_projetos(workspace: str) -> dict:
    """Varre a pasta workspace e retorna todos os diretórios como projetos."""
    projetos = {}
    for entry in sorted(os.listdir(workspace)):
        caminho = os.path.join(workspace, entry)
        if os.path.isdir(caminho) and not entry.startswith("."):
            projetos[entry] = {"nome": entry, "path": caminho}
    return projetos

PROJETOS = descobrir_projetos(WORKSPACE)
PROJETO_PADRAO = None

BOTFATHER_COMMANDS = (
    "gitpush - Commit + push\n"
    "cancelar - Cancela o comando em andamento\n"
    "restart_claude - Para todos os processos Claude e limpa sessoes\n"
    "restart_bot - Reinicia o bot\n"
    "limpar_conversa - Limpa conversa do Claude\n"
    "model - Escolhe modelo do Claude (opus, sonnet, haiku)\n"
    "restart_todos - Reinicia todos os bots da maquina\n"
    "reboot_pc - Reinicia o computador\n"
    "gitdiff - Mostra diff e sugere mensagem de commit\n"
    "projeto - Seleciona o projeto ativo\n"
    "bash - Roda um comando no terminal\n"
    "ngrok - Gerencia tunnel ngrok do projeto\n"
    "gitbranch - Troca ou cria branch\n"
    "gitreset - Descarta todas as alteracoes locais\n"
    "ping_pc - Checa se o desktop esta ligado\n"
    "users - Gerenciar usuarios autorizados\n"
    "menu - Exibe este menu"
)

DEFAULT_TIMEOUT = 120
CLAUDE_TIMEOUT = 2400
MAX_STDOUT = 3800          # truncar stdout acima disso
MAX_DIFF = 8000            # truncar diff enviado pro Claude
TELEGRAM_MSG_LIMIT = 4096  # limite do Telegram por mensagem
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB por arquivo de log
LOG_BACKUP_COUNT = 3
