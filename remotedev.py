#!/usr/bin/env python3
"""
Telegram Bot — Controle remoto multiprojeto.
Suporta múltiplas instâncias (bots) rodando em paralelo.

Uso:
  python3 remotedev.py <nome_bot>
  python3 remotedev.py dev
  python3 remotedev.py prod
  python3 remotedev.py dev --get-chat-id

Variáveis de ambiente por bot:
  TELEGRAM_BOT_<NOME>_TOKEN   — token do BotFather
  TELEGRAM_<NOME>_CHAT_ID     — chat_id autorizado
"""

import os
import sys
import subprocess
import asyncio
import html
import json as json_mod
import tempfile
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ══════════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════════════

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
BOT_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

WORKSPACE = os.path.expanduser("~/workspace")

def descobrir_projetos(workspace: str) -> dict:
    """Varre a pasta workspace e retorna todos os diretórios como projetos."""
    projetos = {}
    for entry in sorted(os.listdir(workspace)):
        caminho = os.path.join(workspace, entry)
        if os.path.isdir(caminho) and not entry.startswith("."):
            projetos[entry] = {
                "nome": entry,
                "path": caminho,
            }
    return projetos

PROJETOS = descobrir_projetos(WORKSPACE)

PROJETO_PADRAO = None

BOTFATHER_COMMANDS = (
    "start - Menu de ajuda\n"
    "help - Menu de ajuda\n"
    "new - Nova sessao Claude (limpa contexto)\n"
    "stop - Cancela comando em execucao\n"
    "p - Trocar projeto\n"
    "bash - Executar comando no terminal\n"
    "git - Comandos git\n"
    "gitdiff - Ver alteracoes pendentes\n"
    "gitdiffia - Gerar mensagem de commit com IA\n"
    "gitpush - Add, commit e push automatico\n"
    "gitbranch - Trocar ou criar branch\n"
    "gitreset - Descartar todas as alteracoes\n"
    "ping_pc - Verifica se desktop esta online\n"
    "restart_bot - Reinicia o bot"
)

DEFAULT_TIMEOUT = 120
CLAUDE_TIMEOUT = 600

estado = {}
pendente = {}  # chat_id → mensagem original (Update) pendente após escolha de projeto
claude_processos = {}  # cwd → subprocess.Popen do Claude em execução
claude_locks = {}  # cwd → asyncio.Lock para fila por projeto
claude_cancelado = set()  # cwds com stop ativo — novos comandos na fila são descartados

# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def projeto_ativo(chat_id: int):
    return estado.get(chat_id, PROJETO_PADRAO)


def projeto_config(chat_id: int):
    key = projeto_ativo(chat_id)
    if key is None or key not in PROJETOS:
        return None
    return PROJETOS[key]


def projeto_path(chat_id: int):
    cfg = projeto_config(chat_id)
    return cfg["path"] if cfg else None


def projeto_label(chat_id: int) -> str:
    cfg = projeto_config(chat_id)
    if not cfg:
        return "⚠️ nenhum projeto"
    path = cfg["path"]
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        branch = ""
    if branch:
        return f"📁 {cfg['nome']} ({branch})"
    return f"📁 {cfg['nome']}"


async def exigir_projeto(update: Update) -> bool:
    """Retorna True se tem projeto selecionado. Se não, pede para escolher e salva comando pendente."""
    chat_id = update.effective_chat.id
    if projeto_config(chat_id) is not None:
        return True
    # Salvar mensagem original para re-executar após escolha
    if update.message and update.message.text:
        pendente[chat_id] = update.message.text
    teclado = [
        [InlineKeyboardButton(cfg['nome'], callback_data=f"projeto:{key}")]
        for key, cfg in PROJETOS.items()
    ]
    await update.message.reply_text("Escolha o projeto:", reply_markup=InlineKeyboardMarkup(teclado))
    return False


def autorizado(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id != CHAT_ID:
            if update.message:
                await update.message.reply_text("⛔ Não autorizado.")
            return
        return await func(update, context)
    return wrapper


def rodar(cmd: str, cwd: str = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env={**os.environ, "TERM": "dumb"},
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        truncated = False

        MAX = 3800
        if len(stdout) > MAX:
            stdout = stdout[:MAX] + "\n\n… (truncado)"
            truncated = True

        return {
            "stdout": stdout,
            "stderr": stderr,
            "code": result.returncode,
            "truncated": truncated,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"⏰ Timeout após {timeout}s", "code": -1, "truncated": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "code": -1, "truncated": False}


def formatar_resultado(res: dict, cmd: str, chat_id: int) -> str:
    icon = "✅" if res["code"] == 0 else "❌"
    label = projeto_label(chat_id)
    partes = [f"{icon} [{label}] <code>{html.escape(cmd)}</code>"]

    if res["stdout"]:
        partes.append(f"<pre>{html.escape(res['stdout'])}</pre>")
    if res["stderr"]:
        partes.append(f"⚠️ <pre>{html.escape(res['stderr'])}</pre>")
    if res["code"] != 0:
        partes.append(f"Exit code: {res['code']}")

    return "\n\n".join(partes)


async def enviar_resultado(update: Update, res: dict, cmd: str):
    chat_id = update.effective_chat.id
    texto = formatar_resultado(res, cmd, chat_id)
    msg = update.message or update.callback_query.message
    pedacos = [texto[i:i + 4096] for i in range(0, len(texto), 4096)]
    for pedaco in pedacos:
        try:
            await msg.reply_text(pedaco, parse_mode="HTML")
        except Exception:
            await msg.reply_text(pedaco)


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — PROJETO
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    label = projeto_label(chat_id)

    # Gerar lista de comandos a partir de BOTFATHER_COMMANDS
    comandos_help = "\n".join(
        f"/{line.split(' - ')[0]} — {line.split(' - ')[1]}"
        for line in BOTFATHER_COMMANDS.strip().split("\n")
        if " - " in line
    )
    await update.message.reply_text(
        f"🤖 <b>Bot [{BOT_NOME}] ativo!</b>\n"
        f"Projeto atual: {label}\n\n"
        "<b>Claude Code:</b>\n"
        "Envie texto livre, áudio ou foto → vai pro Claude\n"
        "/new — nova sessão (limpa contexto)\n\n"
        f"<b>Comandos:</b>\n{comandos_help}",
        parse_mode="HTML",
    )


@autorizado
async def cmd_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if context.args:
        key = context.args[0].lower()
        if key in PROJETOS:
            estado[chat_id] = key
            label = projeto_label(chat_id)
            await update.message.reply_text(f"Projeto alterado para {label}")
        else:
            nomes = ", ".join(PROJETOS.keys())
            await update.message.reply_text(f"Projeto não encontrado. Opções: {nomes}")
        return

    atual = projeto_ativo(chat_id)
    teclado = []
    for key, cfg in PROJETOS.items():
        marcador = " ✅" if key == atual else ""
        teclado.append([InlineKeyboardButton(
            f"{cfg['nome']}{marcador}",
            callback_data=f"projeto:{key}",
        )])

    await update.message.reply_text(
        "Selecione o projeto:",
        reply_markup=InlineKeyboardMarkup(teclado),
    )


@autorizado
async def callback_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    key = query.data.replace("projeto:", "")
    if key not in PROJETOS:
        return

    chat_id = update.effective_chat.id
    estado[chat_id] = key
    label = projeto_label(chat_id)

    # Verificar se há comando pendente
    cmd_pendente = pendente.pop(chat_id, None)
    if cmd_pendente:
        await query.edit_message_text(f"Projeto: {label}\n⏳ Retomando: {cmd_pendente}")
        # Re-processar o comando pendente
        await processar_comando(chat_id, cmd_pendente, query.message, context)
    else:
        await query.edit_message_text(f"Projeto alterado para {label}")


async def processar_comando(chat_id, texto, msg, context):
    """Re-processa um comando pendente após seleção de projeto."""
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    if texto.startswith("/c ") or texto.startswith("/claude ") or texto.startswith("/cc "):
        prompt = texto.split(" ", 1)[1] if " " in texto else ""
        if not prompt:
            return
        await _rodar_claude_completo(msg, chat_id, prompt)

    elif texto.startswith("/bash "):
        cmd = texto.split(" ", 1)[1]
        await msg.reply_text("⏳ Executando...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/gitpush"):
        msg_commit = texto.split(" ", 1)[1].strip() if " " in texto else ""
        if not msg_commit:
            status = rodar("git status --short", cwd=cwd)
            if not status["stdout"]:
                await msg.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
                return
            await msg.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
            _, msg_commit = await _gerar_commit_ia(cwd)
            if not msg_commit:
                await msg.reply_text("⚠️ Não consegui gerar mensagem de commit.")
                return
        await msg.reply_text(f"⏳ Push: {msg_commit}")
        res = rodar("git add -A", cwd=cwd)
        if res["code"] == 0:
            res = rodar(f'git commit -m "{msg_commit}"', cwd=cwd)
            if res["code"] == 0:
                res = git_push(cwd)
                await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")
                await pos_push(msg, cwd, res)
                return
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/gitdiffia"):
        await _enviar_diff(msg, cwd, label)
        await msg.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
        resumo, msg_commit = await _gerar_commit_ia(cwd)
        texto_resp = ""
        if resumo:
            texto_resp += f"📝 Resumo: {resumo}\n\n"
        texto_resp += f"💡 Commit: {msg_commit or '(sem sugestão)'}"
        await msg.reply_text(texto_resp)

    elif texto.startswith("/gitdiff"):
        await _enviar_diff(msg, cwd, label)

    elif texto.startswith("/git"):
        args = texto.split(" ", 1)[1] if " " in texto else "status"
        cmd = f"git {args}"
        await msg.reply_text(f"⏳ git {args}...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    else:
        # Mensagem livre → Claude
        await _rodar_claude_completo(msg, chat_id, texto)


async def _rodar_claude_completo(msg, chat_id, prompt):
    """Executa Claude com sessão, log, hooks e resposta. Fila por projeto."""
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    # Fila por projeto — um comando de cada vez
    if cwd not in claude_locks:
        claude_locks[cwd] = asyncio.Lock()
    lock = claude_locks[cwd]

    if lock.locked():
        # Comando chegou enquanto outro roda — entra na fila
        # Se stop já foi chamado, descartar imediatamente
        if cwd in claude_cancelado:
            return
        await msg.reply_text(f"⏳ Aguardando comando anterior... [{label}]")

    async with lock:
        # Se /stop foi chamado enquanto esperava na fila, descartar
        if cwd in claude_cancelado:
            claude_cancelado.discard(cwd)
            return

        session_id = claude_sessions.get(cwd)

        if session_id:
            await msg.reply_text(f"🧠 Claude (continuando)... [{label}]")
        else:
            await msg.reply_text(f"🧠 Claude (nova sessão)... [{label}]")

        log_prefix = "(continuação) " if session_id else ""
        logar_prompt(label, cwd, f"{log_prefix}{prompt}")

        hash_antes = git_remote_hash(cwd)
        res, texto_resposta, novo_session_id = await asyncio.to_thread(rodar_claude, prompt, cwd, session_id)

        if novo_session_id:
            claude_sessions[cwd] = novo_session_id

        logar_claude(label, cwd, f"{log_prefix}{prompt}", res, texto_resposta)

        await msg.reply_text(texto_resposta or "(sem resposta)")
        eventos = detectar_eventos(cwd, hash_antes)
        hooks_msgs = executar_hooks(cwd, eventos)
        for h in hooks_msgs:
            await msg.reply_text(h)


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — EXECUÇÃO
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_meu_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Seu chat_id: <code>{update.effective_chat.id}</code>",
        parse_mode="HTML",
    )


@autorizado
async def cmd_ping_pc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    label = projeto_label(update.effective_chat.id)
    await update.message.reply_text(f"🟢 Online [{BOT_NOME}] — {agora}\nProjeto: {label}")


@autorizado
async def cmd_bash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = " ".join(context.args) if context.args else ""
    if not cmd:
        await update.message.reply_text("Uso: /bash <comando>")
        return

    if not await exigir_projeto(update):
        return

    await update.message.reply_text("⏳ Executando...")
    res = rodar(cmd, cwd=projeto_path(update.effective_chat.id))
    await enviar_resultado(update, res, cmd)


# Sessões do Claude por projeto (para /cc continuar conversa)
claude_sessions = {}

def carregar_hooks(cwd):
    """Carrega hooks do .remotedev.json do projeto."""
    config_path = os.path.join(cwd, ".remotedev.json")
    if not os.path.exists(config_path):
        return []
    try:
        with open(config_path) as f:
            data = json_mod.load(f)
        return data.get("hooks", [])
    except (json_mod.JSONDecodeError, OSError):
        return []


def git_remote_hash(cwd):
    """Retorna o hash do remote origin/main (ou HEAD se falhar)."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return res.stdout.strip() if res.returncode == 0 else None
    except Exception:
        return None


def detectar_eventos(cwd, hash_antes):
    """Detecta eventos comparando estado do git antes e depois."""
    eventos = set()
    if hash_antes:
        # Atualizar referências remotas
        subprocess.run(["git", "fetch", "--quiet"], cwd=cwd, capture_output=True, timeout=10)
        hash_depois = git_remote_hash(cwd)
        if hash_depois and hash_depois != hash_antes:
            eventos.add("git_pushed")
    return eventos


def git_push(cwd, timeout=60):
    """Faz git push, com fallback para -u origin <branch> se não tiver upstream."""
    res = rodar("git push", cwd=cwd, timeout=timeout)
    if res["code"] != 0 and "no upstream branch" in (res["stderr"] or ""):
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if branch:
            res = rodar(f"git push -u origin {branch}", cwd=cwd, timeout=timeout)
    return res


def executar_hooks(cwd, eventos):
    """Verifica hooks do projeto e executa os que casam com eventos detectados."""
    hooks = carregar_hooks(cwd)
    resultados = []
    for hook in hooks:
        trigger = hook.get("trigger", "")
        if trigger and trigger in eventos:
            run_cmd = hook.get("run", "")
            msg = hook.get("msg", f"Hook executado: {run_cmd}")
            if run_cmd:
                rodar(run_cmd, cwd=cwd, timeout=30)
                resultados.append(msg)
    return resultados


async def pos_push(update_or_msg, cwd, res):
    """Hooks e auto-restart após push bem-sucedido."""
    msg = update_or_msg.message if hasattr(update_or_msg, 'message') else update_or_msg
    if res["code"] == 0:
        for h in executar_hooks(cwd, {"git_pushed"}):
            await msg.reply_text(h)
        if os.path.realpath(cwd) == os.path.realpath(BOT_REPO_DIR):
            await msg.reply_text(f"🔄 Reiniciando {BOT_NOME}...")
            subprocess.Popen(f"sleep 2 && systemctl --user restart {BOT_SERVICE}", shell=True)


def rodar_claude(prompt, cwd, session_id=None):
    """Roda o Claude via stdin e retorna (res, texto_resposta, session_id)."""

    flags = ['--dangerously-skip-permissions', '--output-format', 'json']
    cmd_args = ['claude', '-p', '-'] + flags
    if session_id:
        cmd_args += ['--resume', session_id]

    # Usar Popen para poder cancelar via /stop
    try:
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

        stdout = stdout.strip()
        stderr = stderr.strip()
        truncated = False
        MAX = 3800
        if len(stdout) > MAX:
            stdout = stdout[:MAX] + "\n\n… (truncado)"
            truncated = True
        res = {"stdout": stdout, "stderr": stderr, "code": proc.returncode, "truncated": truncated}

        # Se foi morto por signal (stop/kill), retornar mensagem limpa
        if proc.returncode and proc.returncode < 0:
            return res, "🛑 Comando cancelado.", None

    except Exception as e:
        claude_processos.pop(cwd, None)
        res = {"stdout": "", "stderr": str(e), "code": -1, "truncated": False}

    # Guardar saída bruta para verificação de hooks
    res["_raw"] = res["stdout"]

    texto_resposta = ""
    novo_session_id = None
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
    except (json_mod.JSONDecodeError, TypeError, KeyError):
        texto_resposta = res["stdout"]

    if not texto_resposta:
        texto_resposta = "(sem resposta)"

    return res, texto_resposta, novo_session_id

LOG_FILE_CLAUDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"claude-{BOT_NOME}.log")


def logar_prompt(label, cwd, prompt):
    """Loga o prompt imediatamente (antes do Claude processar)."""
    with open(LOG_FILE_CLAUDE, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {label}\n")
        f.write(f"Projeto: {cwd}\n")
        f.write(f"Prompt: {prompt}\n")
        f.write(f"⏳ Aguardando Claude...\n")


def logar_claude(label, cwd, prompt, res, texto_resposta):
    """Salva resposta do Claude no log (complementa o logar_prompt)."""
    with open(LOG_FILE_CLAUDE, "a") as f:
        f.write(f"Exit: {res['code']}\n")
        if texto_resposta:
            f.write(f"Resposta:\n{texto_resposta}\n")
        if res["stderr"]:
            f.write(f"Erro:\n{res['stderr']}\n")


async def enviar_para_claude(update: Update, prompt: str):
    """Handler unificado do Claude — delega para _rodar_claude_completo."""
    await _rodar_claude_completo(update.message, update.effective_chat.id, prompt)



@autorizado
async def cmd_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)
    claude_sessions.pop(cwd, None)
    await update.message.reply_text(f"✅ Nova sessão iniciada! [{label}]")


@autorizado
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)
    proc = claude_processos.get(cwd)

    # Marcar como cancelado — descarta comandos na fila
    claude_cancelado.add(cwd)

    if proc and proc.poll() is None:
        await update.message.reply_text(f"⏳ Cancelando... [{label}]")
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass
        claude_processos.pop(cwd, None)
        await update.message.reply_text(f"🛑 Tudo cancelado! [{label}]")
    else:
        await update.message.reply_text(f"🛑 Fila limpa! [{label}]")


@autorizado
async def cmd_gitreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpa todas as alterações pendentes (checkout + clean)."""
    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)

    rodar("git checkout .", cwd=cwd)
    rodar("git clean -fd", cwd=cwd)
    await update.message.reply_text(f"🗑️ Todas as alterações descartadas! [{label}]")


def _obter_diff_texto(cwd):
    """Retorna o diff completo do projeto (staged + unstaged + untracked)."""
    diff_out = rodar("git diff", cwd=cwd)
    diff_cached = rodar("git diff --cached", cwd=cwd)
    diff_texto = (diff_out["stdout"] or "") + "\n" + (diff_cached["stdout"] or "")
    diff_texto = diff_texto.strip()

    if not diff_texto:
        status = rodar("git status --short", cwd=cwd)
        diff_texto = status["stdout"] or ""

    if len(diff_texto) > 8000:
        diff_texto = diff_texto[:8000] + "\n... (diff truncado)"

    return diff_texto


async def _gerar_commit_ia(cwd):
    """Gera mensagem de commit via Claude AI baseada no diff atual. Retorna (resumo, msg_commit)."""
    diff_texto = _obter_diff_texto(cwd)
    if not diff_texto:
        return None, None

    prompt = (
        "Analise o diff abaixo e responda EXATAMENTE neste formato (sem nada antes ou depois):\n"
        "RESUMO: <resumo curto em português do que foi feito, 1-2 frases, linguagem natural>\n"
        "COMMIT: <mensagem de commit no formato tipo(escopo): descrição em português>\n\n"
        "Tipos de commit: feat, fix, refactor, docs, style, chore, test.\n\n"
        f"{diff_texto}"
    )

    res, texto_resposta, _ = await asyncio.to_thread(rodar_claude, prompt, cwd)

    resposta = texto_resposta.strip()
    resumo = ""
    msg_commit = ""
    for linha in resposta.split("\n"):
        linha = linha.strip()
        if linha.upper().startswith("RESUMO:"):
            resumo = linha[7:].strip()
        elif linha.upper().startswith("COMMIT:"):
            msg_commit = linha[7:].strip().strip('"').strip("'").strip("`")

    if not msg_commit:
        msg_commit = resposta.strip().strip('"').strip("'").strip("`")

    return resumo, msg_commit


@autorizado
async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faz add, commit e push. Se não passar mensagem, gera via IA."""
    if not await exigir_projeto(update):
        return

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)
    msg_commit = " ".join(context.args).strip() if context.args else ""

    # Se não passou mensagem, gerar via IA
    if not msg_commit:
        status = rodar("git status --short", cwd=cwd)
        if not status["stdout"]:
            await update.message.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
            return
        await update.message.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
        resumo, msg_commit = await _gerar_commit_ia(cwd)
        if not msg_commit:
            await update.message.reply_text("⚠️ Não consegui gerar mensagem de commit.")
            return

    await update.message.reply_text(f"⏳ Push: {msg_commit}")

    # git add -A
    res = rodar("git add -A", cwd=cwd)
    if res["code"] != 0:
        await enviar_resultado(update, res, "git add -A")
        return

    # git commit
    cmd_commit = f'git commit -m "{msg_commit}"'
    res = rodar(cmd_commit, cwd=cwd)
    if res["code"] != 0:
        # Verificar se é "nothing to commit"
        if "nothing to commit" in (res["stdout"] + res["stderr"]):
            await update.message.reply_text("⚠️ Nada para commitar.")
            return
        await enviar_resultado(update, res, cmd_commit)
        return

    # git push
    res = git_push(cwd)
    await enviar_resultado(update, res, f"git push ({msg_commit})")
    await pos_push(update, cwd, res)


@autorizado
async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return

    args = " ".join(context.args) if context.args else "status"
    cmd = f"git {args}"

    await update.message.reply_text(f"⏳ git {args}...")
    res = rodar(cmd, cwd=projeto_path(update.effective_chat.id))
    await enviar_resultado(update, res, cmd)


@autorizado
async def cmd_gitbranch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Troca de branch (cria se não existir) e faz git pull."""
    if not await exigir_projeto(update):
        return

    branch = " ".join(context.args).strip() if context.args else ""
    if not branch:
        # Sem argumento: mostra branch atual e lista
        cwd = projeto_path(update.effective_chat.id)
        res = rodar("git branch -a", cwd=cwd)
        await enviar_resultado(update, res, "git branch -a")
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)

    # Tentar trocar para branch existente
    res = rodar(f"git checkout {branch}", cwd=cwd)
    if res["code"] != 0:
        # Branch não existe — criar
        res = rodar(f"git checkout -b {branch}", cwd=cwd)
        if res["code"] != 0:
            await enviar_resultado(update, res, f"git checkout -b {branch}")
            return
        await update.message.reply_text(f"🌿 [{label}] Branch <code>{branch}</code> criada!", parse_mode="HTML")
    else:
        await update.message.reply_text(f"🔀 [{label}] Branch: <code>{branch}</code>", parse_mode="HTML")

    # git pull
    res_pull = rodar("git pull", cwd=cwd, timeout=60)
    if res_pull["code"] == 0 and res_pull["stdout"]:
        await update.message.reply_text(f"⬇️ {res_pull['stdout']}")




async def _enviar_diff(msg, cwd, label):
    """Mostra alterações pendentes do git."""
    status = rodar("git status --short", cwd=cwd)
    if not status["stdout"]:
        await msg.reply_text(f"✅ [{label}] Nenhuma alteração pendente.")
        return

    linhas = [l for l in status["stdout"].split("\n") if l.strip()]
    added = [l for l in linhas if l.startswith("?") or l.startswith("A")]
    modified = [l for l in linhas if l.startswith("M") or l.startswith(" M")]
    deleted = [l for l in linhas if l.startswith("D") or l.startswith(" D")]

    resumo = []
    if added:
        resumo.append(f"📄 {len(added)} novo(s)")
    if modified:
        resumo.append(f"✏️ {len(modified)} modificado(s)")
    if deleted:
        resumo.append(f"🗑️ {len(deleted)} removido(s)")

    diff_stat = rodar("git diff --stat", cwd=cwd)
    texto = f"📊 [{label}] {', '.join(resumo)}\n\n"
    texto += f"<pre>{html.escape(status['stdout'])}</pre>"
    if diff_stat["stdout"]:
        texto += f"\n\n<pre>{html.escape(diff_stat['stdout'])}</pre>"

    try:
        await msg.reply_text(texto, parse_mode="HTML")
    except Exception:
        await msg.reply_text(texto)


@autorizado
async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return
    chat_id = update.effective_chat.id
    await _enviar_diff(update.message, projeto_path(chat_id), projeto_label(chat_id))


@autorizado
async def cmd_diffia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra diff + gera resumo e mensagem de commit via IA."""
    if not await exigir_projeto(update):
        return
    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    status = rodar("git status --short", cwd=cwd)
    if not status["stdout"]:
        await update.message.reply_text(f"✅ [{label}] Nenhuma alteração pendente.")
        return

    # Mostrar diff primeiro
    await _enviar_diff(update.message, cwd, label)

    # Gerar commit via IA
    aguarde = await update.message.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
    resumo, msg_commit = await _gerar_commit_ia(cwd)

    try:
        await aguarde.delete()
    except Exception:
        pass

    texto = ""
    if resumo:
        texto += f"📝 <b>Resumo:</b> {html.escape(resumo)}\n\n"
    texto += f"💡 <b>Commit:</b>\n<code>{html.escape(msg_commit or '(sem sugestão)')}</code>"

    try:
        await update.message.reply_text(texto, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(texto)




@autorizado
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Reiniciando {BOT_NOME}...")
    subprocess.Popen(
        f"sleep 2 && systemctl --user restart {BOT_SERVICE}",
        shell=True,
    )


@autorizado
async def mensagem_livre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto:
        return

    if not await exigir_projeto(update):
        return

    await enviar_para_claude(update, texto)


async def transcrever_audio(file_path: str) -> str:
    """Transcreve áudio usando OpenAI Whisper API."""
    from openai import OpenAI
    client = OpenAI()
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
        )
    return transcription.text


@autorizado
async def mensagem_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        await update.message.reply_text("⚠️ OPENAI_API_KEY não configurada. Áudios não podem ser transcritos.\nRode ./bot.sh install novamente para configurar.")
        return

    await update.message.reply_text("🎤 Transcrevendo áudio...")

    tmp_path = None
    try:
        # Baixar áudio do Telegram
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
            await file.download_to_drive(tmp_path)

        # Transcrever
        texto = await transcrever_audio(tmp_path)
        os.unlink(tmp_path)
        tmp_path = None

        if not texto or not texto.strip():
            await update.message.reply_text("⚠️ Não consegui transcrever o áudio.")
            return

        await update.message.reply_text(f"📝 {texto}")

        # Verificar projeto APÓS transcrever — salva texto como pendente
        chat_id = update.effective_chat.id
        if projeto_config(chat_id) is None:
            pendente[chat_id] = texto
            if not await exigir_projeto(update):
                return

        await enviar_para_claude(update, texto)

    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        await update.message.reply_text(f"❌ Erro ao transcrever: {e}")


@autorizado
async def mensagem_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return

    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    caption = update.message.caption or ""

    try:
        # Baixar a foto (maior resolução)
        photo = update.message.photo[-1]
        file = await photo.get_file()
        img_name = f"telegram_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        img_path = os.path.join(tempfile.gettempdir(), img_name)
        await file.download_to_drive(img_path)

        # Montar prompt para o Claude analisar a imagem
        if caption:
            prompt = f"Analise a imagem em {img_path} e responda: {caption}"
        else:
            prompt = f"Leia a imagem em {img_path}. Se for um erro ou bug, sugira a correção. Se for código, analise. Seja direto e objetivo, sem descrever a imagem."

        await enviar_para_claude(update, prompt)

        # Limpar imagem após envio
        if os.path.exists(img_path):
            os.unlink(img_path)

    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao processar imagem: {e}")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    print(f"🤖 Bot [{BOT_NOME}] iniciando...")
    print(f"📁 Projetos ({len(PROJETOS)}): {', '.join(PROJETOS.keys())}")
    print(f"🔐 Chat ID autorizado: {CHAT_ID}")

    if "--get-chat-id" in sys.argv:
        if not TOKEN:
            print(f"\n⚠️  Configure TELEGRAM_BOT_{BOT_NOME.upper()}_TOKEN primeiro!")
            print("   Veja o README para instruções.")
            return

        print("\n📱 Mande qualquer mensagem pro bot no Telegram...")

        async def mostrar_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
            cid = update.effective_chat.id
            print(f"\n✅ Seu CHAT_ID: {cid}\n")
            await update.message.reply_text(f"Seu chat_id: {cid}\nColoque no script e reinicie.")

        app = Application.builder().token(TOKEN).build()
        app.add_handler(MessageHandler(filters.ALL, mostrar_id))
        app.run_polling()
        return

    if not TOKEN or CHAT_ID == 0:
        print(f"\n⚠️  Configure as variáveis para o bot '{BOT_NOME}':")
        print(f"   TELEGRAM_BOT_{BOT_NOME.upper()}_TOKEN")
        print(f"   TELEGRAM_{BOT_NOME.upper()}_CHAT_ID")
        print("   Veja o README para instruções.")
        return

    app = Application.builder().token(TOKEN).concurrent_updates(True).build()

    # Projeto
    app.add_handler(CommandHandler("p", cmd_projeto))
    app.add_handler(CommandHandler("projeto", cmd_projeto))
    app.add_handler(CallbackQueryHandler(callback_projeto, pattern=r"^projeto:"))

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("ping_pc", cmd_ping_pc))
    app.add_handler(CommandHandler("bash", cmd_bash))
    app.add_handler(CommandHandler("new", cmd_new_session))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("gitdiff", cmd_diff))
    app.add_handler(CommandHandler("gitdiffia", cmd_diffia))
    app.add_handler(CommandHandler("gitreset", cmd_gitreset))
    app.add_handler(CommandHandler("gitbranch", cmd_gitbranch))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("gitpush", cmd_push))
    app.add_handler(CommandHandler("restart_bot", cmd_restart))

    # Mensagem livre → Claude
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_livre))

    # Áudio → transcrever → Claude
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, mensagem_audio))

    # Foto → Claude analisa
    app.add_handler(MessageHandler(filters.PHOTO, mensagem_foto))

    async def post_init(application):
        # Registrar comandos automaticamente
        from telegram import BotCommand
        commands = []
        for line in BOTFATHER_COMMANDS.strip().split("\n"):
            if " - " in line:
                cmd, desc = line.split(" - ", 1)
                commands.append(BotCommand(cmd.strip(), desc.strip()))
        await application.bot.set_my_commands(commands)

        teclado = [
            [InlineKeyboardButton(cfg['nome'], callback_data=f"projeto:{key}")]
            for key, cfg in PROJETOS.items()
        ]
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🟢 {BOT_NOME} iniciado!\nEscolha o projeto:",
            reply_markup=InlineKeyboardMarkup(teclado),
        )

    app.post_init = post_init
    print("✅ Bot rodando! Ctrl+C pra parar.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
