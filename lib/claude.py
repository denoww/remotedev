import os
import subprocess
import asyncio
import json as json_mod
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from lib.config import BOT_NOME, BOT_REPO_DIR, CLAUDE_TIMEOUT, LOG_MAX_BYTES, LOG_BACKUP_COUNT, TELEGRAM_MSG_LIMIT
from lib.utils import rodar, projeto_path, projeto_label
from lib.hooks import git_remote_hash, detectar_eventos, executar_hooks

# Estado
claude_sessions = {}  # cwd → session_id
claude_processos = {}  # cwd → subprocess.Popen
claude_locks = {}  # cwd → asyncio.Lock
claude_cancelado = set()  # cwds com stop ativo

# Lock file para sinalizar que Claude está rodando (evita restart durante execução)
CLAUDE_LOCK_FILE = f"/tmp/remotedev-claude-{BOT_NOME}.lock"


def _criar_lock():
    """Cria lock file indicando que o Claude está em execução."""
    try:
        with open(CLAUDE_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remover_lock():
    """Remove lock file após execução do Claude."""
    try:
        os.remove(CLAUDE_LOCK_FILE)
    except OSError:
        pass

# Logger com rotação
LOG_FILE_CLAUDE = os.path.join(BOT_REPO_DIR, f"claude-{BOT_NOME}.log")
_claude_logger = logging.getLogger(f"claude-{BOT_NOME}")
_claude_logger.setLevel(logging.INFO)
_claude_handler = RotatingFileHandler(LOG_FILE_CLAUDE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
_claude_handler.setFormatter(logging.Formatter("%(message)s"))
_claude_logger.addHandler(_claude_handler)


def logar_prompt(label, cwd, prompt):
    _claude_logger.info(f"\n{'='*60}")
    _claude_logger.info(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {label}")
    _claude_logger.info(f"Projeto: {cwd}")
    _claude_logger.info(f"Prompt: {prompt}")
    _claude_logger.info(f"⏳ Aguardando Claude...")


def logar_claude(label, cwd, prompt, res, texto_resposta):
    _claude_logger.info(f"Exit: {res['code']}")
    if texto_resposta:
        _claude_logger.info(f"Resposta:\n{texto_resposta}")
    if res["stderr"]:
        _claude_logger.info(f"Erro:\n{res['stderr']}")


def rodar_claude(prompt, cwd, session_id=None):
    """Roda o Claude via stdin e retorna (res, texto_resposta, session_id)."""
    system_prompt = (
        "Nunca use tabelas Markdown (sintaxe `| col |`). "
        "Use listas com `-` ou texto corrido no lugar de tabelas."
    )
    flags = ['--dangerously-skip-permissions', '--output-format', 'json', '--verbose',
             '--system-prompt', system_prompt]
    cmd_args = ['claude', '-p', '-'] + flags
    if session_id:
        cmd_args += ['--resume', session_id]

    try:
        _criar_lock()
        proc = subprocess.Popen(
            cmd_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, text=True, env={**os.environ, "TERM": "dumb"},
            start_new_session=True,
        )
        claude_processos[cwd] = proc
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=CLAUDE_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), 9)
            stdout, stderr = proc.communicate()
        finally:
            claude_processos.pop(cwd, None)
            _remover_lock()

        stdout = stdout.strip()
        stderr = stderr.strip()
        res = {"stdout": stdout, "stderr": stderr, "code": proc.returncode, "truncated": False}

        if proc.returncode and proc.returncode < 0:
            return res, "🛑 Comando cancelado.", None

    except Exception as e:
        claude_processos.pop(cwd, None)
        _remover_lock()
        res = {"stdout": "", "stderr": str(e), "code": -1, "truncated": False}

    res["_raw"] = res["stdout"]

    texto_resposta = ""
    novo_session_id = None
    thinking = []
    tools_usadas = []
    try:
        data = json_mod.loads(res["stdout"])
        if isinstance(data, dict):
            texto_resposta = data.get("result") or data.get("text") or ""
            novo_session_id = data.get("session_id")
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if item.get("type") == "result":
                        texto_resposta = item.get("result") or item.get("text") or ""
                    if item.get("session_id"):
                        novo_session_id = item.get("session_id")
                    # Extrair thinking e tools do verbose
                    if item.get("type") == "assistant":
                        msg = item.get("message", {})
                        for block in msg.get("content", []):
                            if isinstance(block, dict):
                                if block.get("type") == "thinking":
                                    t = block.get("thinking", "").strip()
                                    if t:
                                        thinking.append(t)
                                elif block.get("type") == "tool_use":
                                    name = block.get("name", "?")
                                    inp = block.get("input", {})
                                    if isinstance(inp, dict):
                                        detalhe = inp.get("command") or inp.get("pattern") or inp.get("file_path") or ""
                                        tools_usadas.append(f"{name}: {detalhe}" if detalhe else name)
                                    else:
                                        tools_usadas.append(name)
    except (json_mod.JSONDecodeError, TypeError, KeyError):
        texto_resposta = res["stdout"]

    # Salvar thinking e tools no log
    if thinking:
        _claude_logger.info(f"🧠 Thinking:\n{'---\n'.join(thinking)}")
    if tools_usadas:
        _claude_logger.info(f"🔧 Tools: {', '.join(tools_usadas)}")

    if not texto_resposta:
        texto_resposta = "(sem resposta)"

    return res, texto_resposta, novo_session_id


async def rodar_claude_completo(msg, chat_id, prompt):
    """Executa Claude com sessão, log, hooks e resposta. Fila por projeto."""
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    if cwd not in claude_locks:
        claude_locks[cwd] = asyncio.Lock()
    lock = claude_locks[cwd]

    enfileirado = lock.locked()

    if enfileirado:
        if cwd in claude_cancelado:
            return
        await msg.reply_text(f"⏳ Aguardando comando anterior... [{label}]")

    async with lock:
        if enfileirado and cwd in claude_cancelado:
            claude_cancelado.discard(cwd)
            return

        session_id = claude_sessions.get(cwd)

        await msg.reply_text(f"⏳ {label}...")

        log_prefix = "(continuação) " if session_id else ""
        logar_prompt(label, cwd, f"{log_prefix}{prompt}")

        hash_antes = git_remote_hash(cwd)
        res, texto_resposta, novo_session_id = await asyncio.to_thread(rodar_claude, prompt, cwd, session_id)

        if novo_session_id:
            claude_sessions[cwd] = novo_session_id
        elif res["code"] is not None and res["code"] < 0:
            # Processo foi morto (timeout ou /stop) — limpa sessão para não retomar contexto pesado
            claude_sessions.pop(cwd, None)

        logar_claude(label, cwd, f"{log_prefix}{prompt}", res, texto_resposta)

        texto = texto_resposta or "(sem resposta)"
        pedacos = [texto[i:i + TELEGRAM_MSG_LIMIT] for i in range(0, len(texto), TELEGRAM_MSG_LIMIT)]
        for pedaco in pedacos:
            await msg.reply_text(pedaco)
        eventos = detectar_eventos(cwd, hash_antes)
        hooks_msgs = executar_hooks(cwd, eventos)
        for h in hooks_msgs:
            await msg.reply_text(h)


async def enviar_para_claude(update, prompt: str):
    """Handler unificado do Claude."""
    await rodar_claude_completo(update.message, update.effective_chat.id, prompt)
