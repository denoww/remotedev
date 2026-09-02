import os
import time
import glob
import threading
import contextlib
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
claude_sessions = {}  # (chat_id, cwd) → session_id
claude_processos = {}  # cwd → subprocess.Popen
claude_inicio = {}  # cwd → epoch do início da execução
claude_locks = {}  # cwd → asyncio.Lock
claude_cancelado = set()  # cwds com stop ativo

# Lock file por bot: sinaliza que há Claude rodando. Serve para os outros bots
# da máquina detectarem execução em andamento antes de um /restart_todos.
CLAUDE_LOCK_FILE = f"/tmp/remotedev-claude-{BOT_NOME}.lock"
CLAUDE_LOCK_GLOB = "/tmp/remotedev-claude-*.lock"

# Arquivo com o modelo escolhido (global por bot). Ausente = padrão do CLI.
MODELO_FILE = os.path.join(BOT_REPO_DIR, f".modelo-{BOT_NOME}.json")

# Modelos aceitos pelo --model do claude CLI (aliases)
MODELOS_VALIDOS = ("opus", "sonnet", "haiku")

# Progresso no Telegram: mínimo entre edições quando há novidade, e batida do
# cronômetro mesmo sem novidade. Segura o volume de edições numa tarefa longa.
INTERVALO_PROGRESSO = 3
HEARTBEAT_PROGRESSO = 30


def carregar_modelo() -> str | None:
    """Retorna modelo salvo (ex: 'opus') ou None para usar padrão do CLI."""
    try:
        with open(MODELO_FILE) as f:
            valor = json_mod.load(f).get("modelo")
        return valor if valor in MODELOS_VALIDOS else None
    except (FileNotFoundError, json_mod.JSONDecodeError, OSError):
        return None


def salvar_modelo(modelo: str | None) -> None:
    """Persiste escolha de modelo. None remove o arquivo (volta ao padrão)."""
    try:
        if modelo is None:
            if os.path.exists(MODELO_FILE):
                os.remove(MODELO_FILE)
            return
        with open(MODELO_FILE, "w") as f:
            json_mod.dump({"modelo": modelo}, f)
    except OSError:
        pass


def _criar_lock():
    """Cria lock file indicando que o Claude está em execução."""
    try:
        with open(CLAUDE_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remover_lock():
    """Remove o lock file, desde que nenhum outro projeto ainda esteja rodando."""
    if any(p and p.poll() is None for p in claude_processos.values()):
        return
    try:
        os.remove(CLAUDE_LOCK_FILE)
    except OSError:
        pass


def claude_em_execucao():
    """Projetos deste bot com Claude vivo agora: [(cwd, segundos_rodando)]."""
    agora = time.time()
    ativos = []
    for cwd, proc in list(claude_processos.items()):
        if proc and proc.poll() is None:
            ativos.append((cwd, int(agora - claude_inicio.get(cwd, agora))))
    return ativos


def bots_com_claude_rodando():
    """Nomes dos bots da máquina com Claude rodando, lidos dos lock files."""
    nomes = []
    for caminho in sorted(glob.glob(CLAUDE_LOCK_GLOB)):
        nome = os.path.basename(caminho)[len("remotedev-claude-"):-len(".lock")]
        if nome:
            nomes.append(nome)
    return nomes


def limpar_sessao(chat_id, cwd):
    """Descarta a sessão do Claude daquele chat naquele projeto."""
    return claude_sessions.pop((chat_id, cwd), None) is not None

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


# ── Progresso ────────────────────────────────────────────────────────────

# Campos de input de ferramenta que valem como descrição no progresso
_CAMPOS_DETALHE = ("command", "pattern", "file_path", "url", "query",
                   "description", "prompt", "text")

# Ferramentas de bastidor: o input não diz nada ao usuário, melhor traduzir
_TOOLS_AMIGAVEIS = {
    "ToolSearch": "preparando ferramentas…",
    "TodoWrite": "organizando o plano…",
}


def _nome_tool(nome):
    """mcp__claude-in-chrome__navigate → chrome/navigate."""
    if nome.startswith("mcp__"):
        partes = nome.split("__")
        if len(partes) >= 3:
            servidor = partes[1].replace("claude-in-chrome", "chrome")
            return f"{servidor}/{partes[2]}"
    return nome


def _detalhe_tool(entrada, limite=90):
    """Primeira linha do campo mais informativo do input da ferramenta."""
    if not isinstance(entrada, dict):
        return ""
    for campo in _CAMPOS_DETALHE:
        valor = entrada.get(campo)
        if isinstance(valor, str) and valor.strip():
            primeira = valor.strip().splitlines()[0]
            return primeira[:limite] + ("…" if len(primeira) > limite else "")
    return ""


def _duracao(segundos):
    segundos = int(segundos)
    if segundos < 60:
        return f"{segundos}s"
    if segundos < 3600:
        return f"{segundos // 60}m{segundos % 60:02d}s"
    return f"{segundos // 3600}h{(segundos % 3600) // 60:02d}m"


class Progresso:
    """Acumula os eventos do stream-json e renderiza o status para o Telegram."""

    def __init__(self, label):
        self.label = label
        self.inicio = time.monotonic()
        self.tools = 0
        self.atual = ""

    def aplicar(self, evento):
        if evento.get("type") != "assistant":
            return
        for bloco in evento.get("message", {}).get("content", []):
            if not isinstance(bloco, dict):
                continue
            tipo = bloco.get("type")
            if tipo == "thinking":
                self.atual = "💭 pensando…"
            elif tipo == "text" and bloco.get("text", "").strip():
                self.atual = "✍️ escrevendo a resposta…"
            elif tipo == "tool_use":
                self.tools += 1
                bruto = bloco.get("name", "?")
                if bruto in _TOOLS_AMIGAVEIS:
                    self.atual = f"🔧 {_TOOLS_AMIGAVEIS[bruto]}"
                    continue
                nome = _nome_tool(bruto)
                detalhe = _detalhe_tool(bloco.get("input"))
                self.atual = f"🔧 {nome}" + (f": {detalhe}" if detalhe else "")

    def assinatura(self):
        """O que, mudando, justifica uma edição imediata da mensagem."""
        return (self.atual, self.tools)

    def render(self, final=False, icone=None):
        marca = icone or ("✅" if final else "⏳")
        linha = f"{marca} {self.label} · {_duracao(time.monotonic() - self.inicio)}"
        if self.tools:
            linha += f" · {self.tools} ação(ões)"
        if final or not self.atual:
            return linha
        return f"{linha}\n{self.atual}"


async def _transmitir_progresso(status, fila, prog):
    """Edita a mensagem de status enquanto os eventos do Claude chegam."""
    assinatura = prog.assinatura()
    ultima_edicao = time.monotonic()
    while True:
        try:
            prog.aplicar(await asyncio.wait_for(fila.get(), timeout=1))
            while not fila.empty():
                prog.aplicar(fila.get_nowait())
        except asyncio.TimeoutError:
            pass

        agora = time.monotonic()
        desde_edicao = agora - ultima_edicao
        novidade = prog.assinatura() != assinatura
        if not (novidade and desde_edicao >= INTERVALO_PROGRESSO) and desde_edicao < HEARTBEAT_PROGRESSO:
            continue

        # 429, "message is not modified", mensagem apagada — nada disso pode
        # derrubar a execução do Claude, que segue em outra thread.
        with contextlib.suppress(Exception):
            await status.edit_text(prog.render())
        assinatura = prog.assinatura()
        ultima_edicao = agora


def quebrar_para_telegram(texto, limite=TELEGRAM_MSG_LIMIT):
    """Divide em mensagens preferindo quebra de linha, depois espaço."""
    pedacos, resto = [], texto
    while len(resto) > limite:
        corte = resto.rfind("\n", 0, limite)
        if corte < limite // 2:
            corte = resto.rfind(" ", 0, limite)
        if corte < limite // 2:
            corte = limite
        pedacos.append(resto[:corte].rstrip())
        resto = resto[corte:].lstrip("\n")
    if resto.strip():
        pedacos.append(resto)
    return pedacos or [texto]


# ── Execução ─────────────────────────────────────────────────────────────

def _matar_processo(proc):
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(proc.pid), 9)


def _extrair_resultado(eventos):
    """Do stream de eventos, tira (texto, session_id, thinking, tools)."""
    texto_resposta = ""
    novo_session_id = None
    thinking, tools_usadas = [], []
    for item in eventos:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "result":
            texto_resposta = item.get("result") or item.get("text") or ""
        if item.get("session_id"):
            novo_session_id = item.get("session_id")
        if item.get("type") == "assistant":
            for bloco in item.get("message", {}).get("content", []):
                if not isinstance(bloco, dict):
                    continue
                if bloco.get("type") == "thinking":
                    t = bloco.get("thinking", "").strip()
                    if t:
                        thinking.append(t)
                elif bloco.get("type") == "tool_use":
                    nome = _nome_tool(bloco.get("name", "?"))
                    detalhe = _detalhe_tool(bloco.get("input"))
                    tools_usadas.append(f"{nome}: {detalhe}" if detalhe else nome)
    return texto_resposta, novo_session_id, thinking, tools_usadas


def rodar_claude(prompt, cwd, session_id=None, on_evento=None):
    """Roda o Claude via stdin e retorna (res, texto_resposta, session_id).

    A saída é lida em stream (`--output-format stream-json`): cada evento vai
    para `on_evento` assim que chega, o que permite mostrar progresso ao vivo.
    Sem `on_evento` o comportamento é o de sempre — bloqueia até o fim.
    """
    system_prompt = (
        "Nunca use tabelas Markdown (sintaxe `| col |`). "
        "Use listas com `-` ou texto corrido no lugar de tabelas.\n"
        "O Chrome desta máquina é o navegador real do usuário, com as sessões já logadas. "
        "Para qualquer site que exija login, use as ferramentas de browser (claude-in-chrome) "
        "em vez de WebFetch. Abra abas novas; não feche as abas do usuário."
    )
    flags = ['--dangerously-skip-permissions', '--chrome',
             '--output-format', 'stream-json', '--verbose',
             '--system-prompt', system_prompt]
    modelo = carregar_modelo()
    if modelo:
        flags += ['--model', modelo]
    cmd_args = ['claude', '-p', '-'] + flags
    if session_id:
        cmd_args += ['--resume', session_id]

    eventos, linhas_cruas, stderr_partes = [], [], []

    try:
        _criar_lock()
        proc = subprocess.Popen(
            cmd_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, text=True, bufsize=1, env={**os.environ, "TERM": "dumb"},
            start_new_session=True,
        )
        claude_processos[cwd] = proc
        claude_inicio[cwd] = time.time()

        # stderr em thread própria: se o pipe enchesse, o Claude travaria
        t_err = threading.Thread(target=lambda: stderr_partes.append(proc.stderr.read() or ""), daemon=True)
        t_err.start()

        # como a leitura agora é incremental, o timeout vira um watchdog
        matador = threading.Timer(CLAUDE_TIMEOUT, _matar_processo, args=(proc,))
        matador.daemon = True
        matador.start()

        try:
            with contextlib.suppress(BrokenPipeError, OSError):
                proc.stdin.write(prompt)
                proc.stdin.close()
            for linha in iter(proc.stdout.readline, ''):
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    evento = json_mod.loads(linha)
                except json_mod.JSONDecodeError:
                    linhas_cruas.append(linha)
                    continue
                eventos.append(evento)
                if on_evento:
                    with contextlib.suppress(Exception):
                        on_evento(evento)
        finally:
            matador.cancel()
            proc.wait()
            t_err.join(timeout=5)
            claude_processos.pop(cwd, None)
            claude_inicio.pop(cwd, None)
            _remover_lock()

        res = {"stdout": "\n".join(linhas_cruas).strip(),
               "stderr": "".join(stderr_partes).strip(),
               "code": proc.returncode, "truncated": False}

        if proc.returncode and proc.returncode < 0:
            res["cancelado"] = True
            res["_raw"] = res["stdout"]
            return res, "🛑 Comando cancelado.", None

    except Exception as e:
        claude_processos.pop(cwd, None)
        claude_inicio.pop(cwd, None)
        _remover_lock()
        res = {"stdout": "", "stderr": str(e), "code": -1, "truncated": False}

    res["_raw"] = res["stdout"]

    texto_resposta, novo_session_id, thinking, tools_usadas = _extrair_resultado(eventos)
    if not texto_resposta:
        texto_resposta = res["stdout"]

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

        chave = (chat_id, cwd)
        session_id = claude_sessions.get(chave)

        prog = Progresso(label)
        status = await msg.reply_text(prog.render())

        log_prefix = "(continuação) " if session_id else ""
        logar_prompt(label, cwd, f"{log_prefix}{prompt}")

        hash_antes = git_remote_hash(cwd)

        # O Claude roda numa thread; os eventos atravessam para o loop via fila,
        # e uma task separada vai editando a mensagem de status.
        fila = asyncio.Queue()
        loop = asyncio.get_running_loop()
        transmissor = asyncio.create_task(_transmitir_progresso(status, fila, prog))
        try:
            res, texto_resposta, novo_session_id = await asyncio.to_thread(
                rodar_claude, prompt, cwd, session_id,
                lambda evento: loop.call_soon_threadsafe(fila.put_nowait, evento),
            )
        finally:
            transmissor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await transmissor

        if res.get("cancelado"):
            icone = "🛑"
        elif res["code"] not in (0, None):
            icone = "⚠️"
        else:
            icone = "✅"
        with contextlib.suppress(Exception):
            await status.edit_text(prog.render(final=True, icone=icone))

        if novo_session_id:
            claude_sessions[chave] = novo_session_id
        # Quando o processo é morto (timeout ou /cancelar), preservamos o session_id
        # atual para que a próxima mensagem retome a conversa. Use /limpar_conversa para começar
        # uma sessão limpa.

        logar_claude(label, cwd, f"{log_prefix}{prompt}", res, texto_resposta)

        texto = texto_resposta or "(sem resposta)"
        if not res.get("cancelado") and res["code"] not in (0, None) and res["stderr"]:
            texto += f"\n\n⚠️ Erro do Claude (exit {res['code']}):\n{res['stderr'][-1500:]}"
        for pedaco in quebrar_para_telegram(texto):
            await msg.reply_text(pedaco)
        eventos = detectar_eventos(cwd, hash_antes)
        hooks_msgs = executar_hooks(cwd, eventos)
        for h in hooks_msgs:
            await msg.reply_text(h)


async def enviar_para_claude(update, prompt: str):
    """Handler unificado do Claude."""
    await rodar_claude_completo(update.message, update.effective_chat.id, prompt)
