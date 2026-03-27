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
import html
import json as json_mod
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

DEFAULT_TIMEOUT = 120
CLAUDE_TIMEOUT = 600

estado = {}
pendente = {}  # chat_id → mensagem original (Update) pendente após escolha de projeto

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
    return f"📁 {cfg['nome']}"


async def exigir_projeto(update: Update) -> bool:
    """Retorna True se tem projeto selecionado. Se não, pede para escolher e salva comando pendente."""
    chat_id = update.effective_chat.id
    if projeto_config(chat_id) is not None:
        return True
    # Salvar mensagem original para re-executar após escolha
    if update.message and update.message.text:
        pendente[chat_id] = update.message.text
    botoes = []
    for key, cfg in PROJETOS.items():
        botoes.append(
            InlineKeyboardButton(f"📁 {cfg['nome']}", callback_data=f"projeto:{key}")
        )
    teclado = InlineKeyboardMarkup([botoes[i:i + 2] for i in range(0, len(botoes), 2)])
    await update.message.reply_text("⚠️ Escolha um projeto primeiro:", reply_markup=teclado)
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

    await update.message.reply_text(
        f"🤖 <b>Bot [{BOT_NOME}] ativo!</b>\n"
        f"Projeto atual: {label}\n\n"
        "<b>Claude Code:</b>\n"
        "Envie texto livre → vai pro Claude (mantém contexto)\n"
        "/new — nova sessão (limpa contexto)\n\n"
        "<b>Projeto:</b>\n"
        "/p — trocar projeto (botões)\n"
        "/p <code>nome</code> — trocar direto\n\n"
        "<b>Comandos:</b>\n"
        "/bash <code>comando</code> — executa no terminal\n"
        "/push <code>msg</code> — add+commit+push (sem msg = auto)\n"
        "/git <code>args</code> — git (pull, push, status...)\n"
        "/ping_pc — verifica se desktop está online\n"
        "/meu_chat_id — mostra seu chat_id\n"
        "/restart — reinicia o bot",
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
    botoes = []
    for key, cfg in PROJETOS.items():
        marcador = " ◀" if key == atual else ""
        botoes.append(
            InlineKeyboardButton(
                f"📁 {cfg['nome']}{marcador}",
                callback_data=f"projeto:{key}",
            )
        )

    teclado = [botoes[i:i + 2] for i in range(0, len(botoes), 2)]
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
        # Delegar para o handler unificado via helper
        await _processar_claude_pendente(msg, cwd, label, prompt)

    elif texto.startswith("/bash "):
        cmd = texto.split(" ", 1)[1]
        await msg.reply_text("⏳ Executando...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/push"):
        msg_commit = texto.split(" ", 1)[1].strip() if " " in texto else ""
        if not msg_commit:
            msg_commit = gerar_mensagem_commit(cwd)
        if not msg_commit:
            await msg.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
            return
        await msg.reply_text(f"⏳ Push: {msg_commit}")
        res = rodar("git add -A", cwd=cwd)
        if res["code"] == 0:
            res = rodar(f'git commit -m "{msg_commit}"', cwd=cwd)
            if res["code"] == 0:
                res = rodar("git push", cwd=cwd, timeout=60)
                await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")
                if res["code"] == 0:
                    for h in executar_hooks(cwd, {"git_pushed"}):
                        await msg.reply_text(h)
                return
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/git"):
        args = texto.split(" ", 1)[1] if " " in texto else "status"
        cmd = f"git {args}"
        await msg.reply_text(f"⏳ git {args}...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    else:
        # Mensagem livre → Claude
        await _processar_claude_pendente(msg, cwd, label, texto)


async def _processar_claude_pendente(msg, cwd, label, prompt):
    """Executa Claude para comandos pendentes (após seleção de projeto)."""
    session_id = claude_sessions.get(cwd)

    if session_id:
        await msg.reply_text(f"🧠 Claude (continuando)... [{label}]")
    else:
        await msg.reply_text(f"🧠 Claude (nova sessão)... [{label}]")

    prompt_escaped = prompt.replace('"', '\\"')
    hash_antes = git_remote_hash(cwd)
    res, texto_resposta, novo_session_id = rodar_claude(prompt_escaped, cwd, session_id)

    if novo_session_id:
        claude_sessions[cwd] = novo_session_id

    log_prefix = "(continuação) " if session_id else ""
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


def rodar_claude(prompt_escaped, cwd, session_id=None):
    """Roda o Claude e retorna (res, texto_resposta, session_id)."""

    flags = '--dangerously-skip-permissions --output-format json'
    if session_id:
        cmd = f'claude -p "{prompt_escaped}" --resume "{session_id}" {flags}'
    else:
        cmd = f'claude -p "{prompt_escaped}" {flags}'

    res = rodar(cmd, cwd=cwd, timeout=CLAUDE_TIMEOUT)

    # Guardar saída bruta para verificação de hooks
    res["_raw"] = res["stdout"]

    texto_resposta = ""
    novo_session_id = None
    try:
        data = json_mod.loads(res["stdout"])
        if isinstance(data, dict):
            texto_resposta = data.get("result", "")
            novo_session_id = data.get("session_id")
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if item.get("type") == "result":
                        texto_resposta = item.get("result", "")
                    if item.get("session_id"):
                        novo_session_id = item.get("session_id")
    except (json_mod.JSONDecodeError, TypeError, KeyError):
        texto_resposta = res["stdout"]

    if not texto_resposta:
        texto_resposta = "(sem resposta)"

    return res, texto_resposta, novo_session_id

def logar_claude(label, cwd, prompt, res, texto_resposta):
    """Salva no log do Claude."""
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"claude-{BOT_NOME}.log")
    with open(log_file, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {label}\n")
        f.write(f"Projeto: {cwd}\n")
        f.write(f"Prompt: {prompt}\n")
        f.write(f"Exit: {res['code']}\n")
        if texto_resposta:
            f.write(f"Resposta:\n{texto_resposta}\n")
        if res["stderr"]:
            f.write(f"Erro:\n{res['stderr']}\n")


async def enviar_para_claude(update: Update, prompt: str):
    """Handler unificado do Claude — mantém sessão por projeto."""
    chat_id = update.effective_chat.id
    label = projeto_label(chat_id)
    cwd = projeto_path(chat_id)
    session_id = claude_sessions.get(cwd)
    msg = update.message

    if session_id:
        await msg.reply_text(f"🧠 Claude (continuando)... [{label}]")
    else:
        await msg.reply_text(f"🧠 Claude (nova sessão)... [{label}]")

    prompt_escaped = prompt.replace('"', '\\"')
    hash_antes = git_remote_hash(cwd)
    res, texto_resposta, novo_session_id = rodar_claude(prompt_escaped, cwd, session_id)

    if novo_session_id:
        claude_sessions[cwd] = novo_session_id

    log_prefix = "(continuação) " if session_id else ""
    logar_claude(label, cwd, f"{log_prefix}{prompt}", res, texto_resposta)

    res["stdout"] = texto_resposta
    await enviar_resultado(update, res, f"claude: {prompt[:80]}...")

    # Executar hooks pós-Claude
    eventos = detectar_eventos(cwd, hash_antes)
    hooks_msgs = executar_hooks(cwd, eventos)
    for h in hooks_msgs:
        await msg.reply_text(h)



@autorizado
async def cmd_new_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)
    claude_sessions.pop(cwd, None)
    await update.message.reply_text(f"✅ Nova sessão iniciada! [{label}]")


def gerar_mensagem_commit(cwd):
    """Gera mensagem de commit em português baseada no git diff."""
    # Pegar diff dos arquivos staged + unstaged
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--stat"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    stat = diff.stdout.strip()
    if not stat:
        # Verificar arquivos não rastreados
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        if untracked.stdout.strip():
            arquivos = untracked.stdout.strip().split("\n")
            if len(arquivos) == 1:
                return f"Adicionar {arquivos[0]}"
            return f"Adicionar {len(arquivos)} novos arquivos"
        return None

    # Pegar diff detalhado para entender as mudanças
    diff_detail = subprocess.run(
        ["git", "diff", "HEAD", "--no-color"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    diff_text = diff_detail.stdout.strip()

    # Analisar arquivos alterados
    changed = subprocess.run(
        ["git", "diff", "HEAD", "--name-only"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    arquivos = [f for f in changed.stdout.strip().split("\n") if f]

    if not arquivos:
        return None

    # Contadores
    added = subprocess.run(
        ["git", "diff", "HEAD", "--diff-filter=A", "--name-only"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    deleted = subprocess.run(
        ["git", "diff", "HEAD", "--diff-filter=D", "--name-only"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    modified = subprocess.run(
        ["git", "diff", "HEAD", "--diff-filter=M", "--name-only"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )

    added_files = [f for f in added.stdout.strip().split("\n") if f]
    deleted_files = [f for f in deleted.stdout.strip().split("\n") if f]
    modified_files = [f for f in modified.stdout.strip().split("\n") if f]

    # Incluir arquivos não rastreados como adicionados
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd, capture_output=True, text=True, timeout=10,
    )
    untracked_files = [f for f in untracked.stdout.strip().split("\n") if f]
    added_files.extend(untracked_files)

    partes = []
    if len(arquivos) + len(untracked_files) == 1:
        nome = (arquivos + untracked_files)[0]
        if added_files:
            return f"Adicionar {nome}"
        if deleted_files:
            return f"Remover {nome}"
        return f"Atualizar {nome}"

    if added_files:
        partes.append(f"adicionar {len(added_files)} arquivo(s)")
    if modified_files:
        partes.append(f"atualizar {len(modified_files)} arquivo(s)")
    if deleted_files:
        partes.append(f"remover {len(deleted_files)} arquivo(s)")

    if partes:
        msg = partes[0].capitalize()
        if len(partes) > 1:
            msg += " e " + ", ".join(partes[1:])
        return msg

    return f"Atualizar {len(arquivos)} arquivo(s)"


@autorizado
async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faz add, commit e push. Se não passar mensagem, gera automaticamente."""
    if not await exigir_projeto(update):
        return

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    msg_commit = " ".join(context.args).strip() if context.args else ""

    # Se não passou mensagem, gerar automaticamente
    if not msg_commit:
        msg_commit = gerar_mensagem_commit(cwd)
        if not msg_commit:
            await update.message.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
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
    res = rodar("git push", cwd=cwd, timeout=60)
    await enviar_resultado(update, res, f"git push ({msg_commit})")

    # Executar hooks de push
    eventos = {"git_pushed"} if res["code"] == 0 else set()
    hooks_msgs = executar_hooks(cwd, eventos)
    for h in hooks_msgs:
        await update.message.reply_text(h)


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
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Reiniciando bot [{BOT_NOME}] em 2s...")
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

    app = Application.builder().token(TOKEN).build()

    # Projeto
    app.add_handler(CommandHandler("p", cmd_projeto))
    app.add_handler(CommandHandler("projeto", cmd_projeto))
    app.add_handler(CallbackQueryHandler(callback_projeto, pattern=r"^projeto:"))

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("meu_chat_id", cmd_meu_chat_id))
    app.add_handler(CommandHandler("ping_pc", cmd_ping_pc))
    app.add_handler(CommandHandler("bash", cmd_bash))
    app.add_handler(CommandHandler("new", cmd_new_session))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # Mensagem livre → Claude
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_livre))

    async def post_init(application):
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🟢 Bot [{BOT_NOME}] iniciado!\n📁 Projetos: {', '.join(PROJETOS.keys())}",
        )

    app.post_init = post_init
    print("✅ Bot rodando! Ctrl+C pra parar.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
