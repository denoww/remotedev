#!/usr/bin/env python3
"""
Telegram Bot — Controle remoto multiprojeto.
Suporta múltiplas instâncias (bots) rodando em paralelo.

Uso:
  python3 telegram_desktop_bot.py <nome_bot>
  python3 telegram_desktop_bot.py dev
  python3 telegram_desktop_bot.py prod
  python3 telegram_desktop_bot.py dev --get-chat-id

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
        print("Uso: python3 telegram_desktop_bot.py <nome_bot>")
        print("  Ex: python3 telegram_desktop_bot.py dev")
        print("  Ex: python3 telegram_desktop_bot.py prod")
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
        "<b>Projeto:</b>\n"
        "/p — trocar projeto (botões)\n"
        "/p <code>nome</code> — trocar direto\n\n"
        "<b>Comandos:</b>\n"
        "/bash <code>comando</code> — executa qualquer comando\n"
        "/c <code>prompt</code> — Claude Code (nova conversa)\n"
        "/cc <code>prompt</code> — Claude Code (continua conversa)\n"
        "/git <code>args</code> — git (pull, push, status...)\n"
        "/rails <code>args</code> — rails runner/console\n"
        "/rake <code>task</code> — rake task\n"
        "/log <code>N</code> — últimas N linhas do log\n"
        "/ping — verifica se desktop está online\n"
        "/id — mostra seu chat_id\n"
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

    if texto.startswith("/c ") or texto.startswith("/claude "):
        # Extrair prompt
        prompt = texto.split(" ", 1)[1] if " " in texto else ""
        if not prompt:
            return
        await msg.reply_text(f"🧠 Claude pensando... [{label}]")
        prompt_escaped = prompt.replace('"', '\\"')
        res, texto_resposta, session_id = rodar_claude(prompt_escaped, cwd)
        if session_id:
            claude_sessions[cwd] = session_id
        logar_claude(label, cwd, prompt, res, texto_resposta)
        res["stdout"] = texto_resposta
        await msg.reply_text(texto_resposta or "(sem resposta)")
        hooks_msgs = executar_hooks(cwd, res.get("_raw", "") or texto_resposta)
        for h in hooks_msgs:
            await msg.reply_text(h)

    elif texto.startswith("/cc "):
        prompt = texto.split(" ", 1)[1] if " " in texto else ""
        if not prompt:
            return
        session_id = claude_sessions.get(cwd)
        if not session_id:
            await msg.reply_text(f"⚠️ Nenhuma conversa anterior em [{label}]. Use /c primeiro.")
            return
        await msg.reply_text(f"🧠 Claude continuando... [{label}]")
        prompt_escaped = prompt.replace('"', '\\"')
        res, texto_resposta, novo_session_id = rodar_claude(prompt_escaped, cwd, session_id)
        if novo_session_id:
            claude_sessions[cwd] = novo_session_id
        logar_claude(label, cwd, f"(continuação) {prompt}", res, texto_resposta)
        res["stdout"] = texto_resposta
        await msg.reply_text(texto_resposta or "(sem resposta)")
        hooks_msgs = executar_hooks(cwd, res.get("_raw", "") or texto_resposta)
        for h in hooks_msgs:
            await msg.reply_text(h)

    elif texto.startswith("/bash "):
        cmd = texto.split(" ", 1)[1]
        await msg.reply_text("⏳ Executando...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/git"):
        args = texto.split(" ", 1)[1] if " " in texto else "status"
        cmd = f"git {args}"
        await msg.reply_text(f"⏳ git {args}...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    else:
        # Mensagem livre → bash
        await msg.reply_text("⏳ Executando...")
        res = rodar(texto, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — EXECUÇÃO
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Seu chat_id: <code>{update.effective_chat.id}</code>",
        parse_mode="HTML",
    )


@autorizado
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


def executar_hooks(cwd, saida_bruta):
    """Verifica hooks do projeto e executa os que casam com a saída do Claude."""
    hooks = carregar_hooks(cwd)
    resultados = []
    for hook in hooks:
        match = hook.get("match", "")
        if match and match in saida_bruta:
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


@autorizado
async def cmd_claude(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Uso: /c <prompt>\nUso: /cc <prompt> — continua última conversa")
        return

    if not await exigir_projeto(update):
        return

    label = projeto_label(update.effective_chat.id)
    cwd = projeto_path(update.effective_chat.id)
    await update.message.reply_text(f"🧠 Claude pensando... [{label}]")

    prompt_escaped = prompt.replace('"', '\\"')
    res, texto_resposta, session_id = rodar_claude(prompt_escaped, cwd)

    # Salvar sessão do projeto para /cc
    if session_id:
        claude_sessions[cwd] = session_id

    logar_claude(label, cwd, prompt, res, texto_resposta)

    res["stdout"] = texto_resposta
    await enviar_resultado(update, res, f"claude: {prompt[:80]}...")

    # Executar hooks pós-Claude
    hooks_msgs = executar_hooks(cwd, res.get("_raw", "") or texto_resposta)
    for msg in hooks_msgs:
        await update.message.reply_text(msg)


@autorizado
async def cmd_claude_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args) if context.args else ""
    if not prompt:
        await update.message.reply_text("Uso: /cc <prompt> — continua última conversa")
        return

    if not await exigir_projeto(update):
        return

    label = projeto_label(update.effective_chat.id)
    cwd = projeto_path(update.effective_chat.id)
    session_id = claude_sessions.get(cwd)

    if not session_id:
        await update.message.reply_text(f"⚠️ Nenhuma conversa anterior em [{label}]. Use /c primeiro.")
        return

    await update.message.reply_text(f"🧠 Claude continuando... [{label}]")

    prompt_escaped = prompt.replace('"', '\\"')
    res, texto_resposta, novo_session_id = rodar_claude(prompt_escaped, cwd, session_id)

    if novo_session_id:
        claude_sessions[cwd] = novo_session_id

    logar_claude(label, cwd, f"(continuação) {prompt}", res, texto_resposta)

    res["stdout"] = texto_resposta
    await enviar_resultado(update, res, f"claude: {prompt[:80]}...")

    # Executar hooks pós-Claude
    hooks_msgs = executar_hooks(cwd, res.get("_raw", "") or texto_resposta)
    for msg in hooks_msgs:
        await update.message.reply_text(msg)


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
async def cmd_rails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text("Uso: /rails runner 'Codigo' ou /rails db:migrate")
        return

    if not await exigir_projeto(update):
        return

    cmd = f"bundle exec rails {args}"
    await update.message.reply_text(f"⏳ rails {args}...")
    res = rodar(cmd, cwd=projeto_path(update.effective_chat.id))
    await enviar_resultado(update, res, cmd)


@autorizado
async def cmd_rake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text("Uso: /rake <task>")
        return

    if not await exigir_projeto(update):
        return

    cmd = f"bundle exec rake {args}"
    await update.message.reply_text(f"⏳ rake {args}...")
    res = rodar(cmd, cwd=projeto_path(update.effective_chat.id))
    await enviar_resultado(update, res, cmd)


@autorizado
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await exigir_projeto(update):
        return

    linhas = context.args[0] if context.args else "50"
    cmd = f"tail -n {linhas} log/development.log"

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

    await update.message.reply_text("⏳ Executando...")
    res = rodar(texto, cwd=projeto_path(update.effective_chat.id))
    await enviar_resultado(update, res, texto)


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
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("bash", cmd_bash))
    app.add_handler(CommandHandler("claude", cmd_claude))
    app.add_handler(CommandHandler("c", cmd_claude))
    app.add_handler(CommandHandler("cc", cmd_claude_continue))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("rails", cmd_rails))
    app.add_handler(CommandHandler("rake", cmd_rake))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("restart", cmd_restart))

    # Mensagem livre → bash
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
