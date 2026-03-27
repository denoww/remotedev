#!/usr/bin/env python3
"""
Telegram Bot — Controle remoto multiprojeto.
Suporta múltiplas instâncias (bots) rodando em paralelo.

Uso:
  python3 remotedev.py <nome_bot>
  python3 remotedev.py dev
  python3 remotedev.py prod
  python3 remotedev.py dev --get-chat-id
"""

import os
import sys
import subprocess
import tempfile
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from lib.config import (
    BOT_NOME, TOKEN, CHAT_ID, BOT_SERVICE,
    PROJETOS, BOTFATHER_COMMANDS,
)
from lib.utils import (
    estado, pendente,
    projeto_ativo, projeto_config, projeto_path, projeto_label,
    exigir_projeto, autorizado, rodar, enviar_resultado,
)
from lib.claude import (
    claude_sessions, claude_cancelado,
    enviar_para_claude, rodar_claude_completo,
)
from lib.git_ops import (
    cmd_diff, cmd_push, cmd_git, cmd_gitbranch, cmd_gitreset,
    callback_branch, _enviar_diff, _gerar_commit_ia, git_push,
)
from lib.hooks import pos_push


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — GERAL
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    label = projeto_label(chat_id)

    comandos_help = "\n".join(
        f"/{line.split(' - ')[0]} — {line.split(' - ')[1]}"
        for line in BOTFATHER_COMMANDS.strip().split("\n")
        if " - " in line
    )
    await update.message.reply_text(
        f"🤖 <b>Bot [{BOT_NOME}] ativo!</b>\n"
        f"Projeto: {label}\n\n"
        "<b>Claude Code</b>\n"
        "Texto, áudio ou foto → Claude responde direto\n\n"
        f"<b>Comandos</b>\n{comandos_help}",
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

    cmd_pendente = pendente.pop(chat_id, None)
    if cmd_pendente:
        await query.edit_message_text(f"Projeto: {label}\n⏳ Retomando: {cmd_pendente}")
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
        await rodar_claude_completo(msg, chat_id, prompt)

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

    elif texto.startswith("/gitdiff"):
        await _enviar_diff(msg, cwd, label)
        await msg.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
        resumo, msg_commit = await _gerar_commit_ia(cwd)
        texto_resp = ""
        if resumo:
            texto_resp += f"📝 Resumo: {resumo}\n\n"
        texto_resp += f"💡 Commit: {msg_commit or '(sem sugestão)'}"
        await msg.reply_text(texto_resp)

    elif texto.startswith("/git"):
        args = texto.split(" ", 1)[1] if " " in texto else "status"
        cmd = f"git {args}"
        await msg.reply_text(f"⏳ git {args}...")
        res = rodar(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    else:
        await rodar_claude_completo(msg, chat_id, texto)


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — EXECUÇÃO
# ══════════════════════════════════════════════════════════════════════

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

    from lib.claude import claude_processos
    proc = claude_processos.get(cwd)

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
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Reiniciando {BOT_NOME}...")
    subprocess.Popen(f"sleep 2 && systemctl --user restart {BOT_SERVICE}", shell=True)


# ══════════════════════════════════════════════════════════════════════
# MENSAGENS — TEXTO, ÁUDIO, FOTO
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def mensagem_livre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto:
        return
    if not await exigir_projeto(update):
        return
    await enviar_para_claude(update, texto)


async def transcrever_audio(file_path: str) -> str:
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
        await update.message.reply_text("⚠️ OPENAI_API_KEY não configurada.\nRode ./bot.sh install para configurar.")
        return

    await update.message.reply_text("🎤 Transcrevendo áudio...")

    tmp_path = None
    try:
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
            await file.download_to_drive(tmp_path)

        texto = await transcrever_audio(tmp_path)
        os.unlink(tmp_path)
        tmp_path = None

        if not texto or not texto.strip():
            await update.message.reply_text("⚠️ Não consegui transcrever o áudio.")
            return

        await update.message.reply_text(f"📝 {texto}")

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
        photo = update.message.photo[-1]
        file = await photo.get_file()
        img_name = f"telegram_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        img_path = os.path.join(tempfile.gettempdir(), img_name)
        await file.download_to_drive(img_path)

        if caption:
            prompt = f"Analise a imagem em {img_path} e responda: {caption}"
        else:
            prompt = f"Leia a imagem em {img_path}. Se for um erro ou bug, sugira a correção. Se for código, analise. Seja direto e objetivo, sem descrever a imagem."

        await enviar_para_claude(update, prompt)

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
        return

    app = Application.builder().token(TOKEN).concurrent_updates(True).build()

    # Projeto
    app.add_handler(CommandHandler("p", cmd_projeto))
    app.add_handler(CommandHandler("projeto", cmd_projeto))
    app.add_handler(CallbackQueryHandler(callback_projeto, pattern=r"^projeto:"))
    app.add_handler(CallbackQueryHandler(callback_branch, pattern=r"^branch:"))

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping_pc", cmd_ping_pc))
    app.add_handler(CommandHandler("bash", cmd_bash))
    app.add_handler(CommandHandler("new", cmd_new_session))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("gitdiff", cmd_diff))
    app.add_handler(CommandHandler("gitreset", cmd_gitreset))
    app.add_handler(CommandHandler("gitbranch", cmd_gitbranch))
    app.add_handler(CommandHandler("git", cmd_git))
    app.add_handler(CommandHandler("gitpush", cmd_push))
    app.add_handler(CommandHandler("restart_bot", cmd_restart))

    # Mensagens
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_livre))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, mensagem_audio))
    app.add_handler(MessageHandler(filters.PHOTO, mensagem_foto))

    async def post_init(application):
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
