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

# ── Multi-user ──────────────────────────────────────────────────────
import json

_USERS_FILE = os.path.join(BOT_REPO_DIR, f".users-{BOT_NOME}.json")


def _carregar_users() -> dict:
    """Carrega usuários autorizados do disco. Retorna {chat_id: {nome, adicionado_por}}."""
    try:
        with open(_USERS_FILE, "r") as f:
            dados = json.load(f)
        return {int(k): v for k, v in dados.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def _salvar_users():
    """Persiste usuários autorizados no disco."""
    try:
        with open(_USERS_FILE, "w") as f:
            json.dump(USERS_AUTORIZADOS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


USERS_AUTORIZADOS: dict = _carregar_users()
# Garante que o owner sempre está na lista
if OWNER_CHAT_ID and OWNER_CHAT_ID not in USERS_AUTORIZADOS:
    USERS_AUTORIZADOS[OWNER_CHAT_ID] = {"nome": "owner", "adicionado_por": "env"}
    _salvar_users()


def chat_ids_autorizados() -> set:
    """Retorna set de todos os chat_ids autorizados."""
    return set(USERS_AUTORIZADOS.keys())


def adicionar_user(chat_id: int, nome: str, adicionado_por: str = "owner"):
    """Adiciona um usuário autorizado."""
    USERS_AUTORIZADOS[chat_id] = {"nome": nome, "adicionado_por": adicionado_por}
    _salvar_users()


def remover_user(chat_id: int) -> bool:
    """Remove um usuário autorizado. Não permite remover o owner."""
    if chat_id == OWNER_CHAT_ID:
        return False
    if chat_id in USERS_AUTORIZADOS:
        del USERS_AUTORIZADOS[chat_id]
        _salvar_users()
        return True
    return False


def is_owner(chat_id: int) -> bool:
    return chat_id == OWNER_CHAT_ID


def is_autorizado(chat_id: int) -> bool:
    return chat_id in USERS_AUTORIZADOS

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
    "restart_bot - Reinicia o bot\n"
    "limpar_conversa - Limpa conversa do Claude\n"
    "restart_todos - Reinicia todos os bots da maquina\n"
    "gitdiff - Mostra diff e sugere mensagem de commit\n"
    "projeto - Seleciona o projeto ativo\n"
    "bash - Roda um comando no terminal\n"
    "gitbranch - Troca ou cria branch\n"
    "gitreset - Descarta todas as alteracoes locais\n"
    "ping_pc - Checa se o desktop esta ligado\n"
    "adduser - Adiciona usuario autorizado\n"
    "removeuser - Remove usuario autorizado\n"
    "users - Lista usuarios autorizados\n"
    "menu - Exibe este menu"
)

DEFAULT_TIMEOUT = 120
CLAUDE_TIMEOUT = 600
MAX_STDOUT = 3800          # truncar stdout acima disso
MAX_DIFF = 8000            # truncar diff enviado pro Claude
TELEGRAM_MSG_LIMIT = 4096  # limite do Telegram por mensagem
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB por arquivo de log
LOG_BACKUP_COUNT = 3
