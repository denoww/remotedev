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
    chat_id = int(os.environ.get(f"TELEGRAM_{nome_upper}_CHAT_ID", "0"))

    return nome, token, chat_id

BOT_NOME, TOKEN, CHAT_ID = carregar_config()
BOT_SERVICE = f"remotedev-{BOT_NOME}"
BOT_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKSPACE = os.path.expanduser("~/workspace")

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
    "start - Exibe este menu\n"
    "new - Inicia sessao limpa do Claude\n"
    "stop - Para o comando em andamento\n"
    "p - Seleciona o projeto ativo\n"
    "bash - Roda um comando no terminal\n"
    "git - Abre o painel git\n"
    "gitdiff - Mostra diff e sugere mensagem de commit\n"
    "gitpush - Faz add, commit e push de uma vez\n"
    "gitbranch - Troca ou cria branch\n"
    "gitreset - Descarta todas as alteracoes locais\n"
    "ping_pc - Checa se o desktop esta ligado\n"
    "restart_bot - Reinicia o bot"
)

DEFAULT_TIMEOUT = 120
CLAUDE_TIMEOUT = 600
MAX_STDOUT = 3800          # truncar stdout acima disso
MAX_DIFF = 8000            # truncar diff enviado pro Claude
TELEGRAM_MSG_LIMIT = 4096  # limite do Telegram por mensagem
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB por arquivo de log
LOG_BACKUP_COUNT = 3
