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
import html
import subprocess
import tempfile
from datetime import datetime
from PIL import Image
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
    BOT_NOME, TOKEN, CHAT_ID, BOT_SERVICE, BOT_REPO_DIR,
    PROJETOS, BOTFATHER_COMMANDS, WORKSPACE, descobrir_projetos,
)
from lib.utils import (
    estado, _salvar_estado, pendente, push_pendente, novo_projeto_pendente, ia_apikey_pendente, ia_modelo_pendente,
    projeto_ativo, projeto_config, projeto_path, projeto_label,
    exigir_projeto, autorizado, rodar, rodar_async, enviar_resultado,
    atualizar_nome_bot,
)
from lib.claude import (
    claude_sessions, claude_cancelado,
    enviar_para_claude, rodar_claude_completo,
)
from lib.git_ops import (
    cmd_diff, cmd_push, cmd_gitbranch, cmd_gitreset,
    callback_branch, callback_push, callback_reset, callback_resumo_diff,
    _enviar_diff, _gerar_commit_ia, git_push,
)
from lib.hooks import pos_push
from lib.novo_projeto import (
    callback_novo_projeto, callback_uso_projeto, callback_github_novo, criar_projeto, validar_nome_projeto,
    callback_ia_analise, callback_ia_provider, callback_ia_modelo, processar_apikey_ia,
)
from lib.excluir_projeto import callback_excluir_projeto, callback_confirmar_exclusao, callback_excluir


# ══════════════════════════════════════════════════════════════════════
# COMANDOS — GERAL
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Texto, áudio, foto ou documento → Claude responde direto\n\n"
        f"<b>Comandos</b>\n{comandos_help}",
        parse_mode="HTML",
    )


@autorizado
async def cmd_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    projetos = descobrir_projetos(WORKSPACE)

    if context.args:
        key = context.args[0].lower()
        if key in projetos:
            estado[chat_id] = key
            _salvar_estado()
            await atualizar_nome_bot(context.bot, chat_id)
            label = projeto_label(chat_id)
            await update.message.reply_text(f"Projeto alterado para {label}")
            await _enviar_diff(update.message, projeto_path(chat_id), label)
        else:
            nomes = ", ".join(projetos.keys())
            await update.message.reply_text(f"Projeto não encontrado. Opções: {nomes}")
        return

    atual = projeto_ativo(chat_id)
    teclado = []
    for key, cfg in projetos.items():
        marcador = " ✅" if key == atual else ""
        teclado.append([InlineKeyboardButton(
            f"{cfg['nome']}{marcador}",
            callback_data=f"projeto:{key}",
        )])
    teclado.append([
        InlineKeyboardButton("➕ Novo Projeto", callback_data="novo_projeto"),
        InlineKeyboardButton("🗑 Excluir Projeto", callback_data="excluir_projeto"),
    ])

    await update.message.reply_text(
        "Selecione o projeto:",
        reply_markup=InlineKeyboardMarkup(teclado),
    )


@autorizado
async def callback_voltar_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Volta para a lista de seleção de projetos."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    projetos = descobrir_projetos(WORKSPACE)
    atual = projeto_ativo(chat_id)
    teclado = []
    for key, cfg in projetos.items():
        marcador = " ✅" if key == atual else ""
        teclado.append([InlineKeyboardButton(
            f"{cfg['nome']}{marcador}",
            callback_data=f"projeto:{key}",
        )])
    teclado.append([
        InlineKeyboardButton("➕ Novo Projeto", callback_data="novo_projeto"),
        InlineKeyboardButton("🗑 Excluir Projeto", callback_data="excluir_projeto"),
    ])
    await query.edit_message_text(
        "Selecione o projeto:",
        reply_markup=InlineKeyboardMarkup(teclado),
    )


@autorizado
async def callback_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    key = query.data.replace("projeto:", "")
    if key not in descobrir_projetos(WORKSPACE):
        return

    chat_id = update.effective_chat.id
    estado[chat_id] = key
    _salvar_estado()
    await atualizar_nome_bot(context.bot, chat_id)
    label = projeto_label(chat_id)

    cmd_pendente = pendente.pop(chat_id, None)
    if cmd_pendente:
        await query.edit_message_text(f"Projeto: {label}\n⏳ Retomando: {cmd_pendente}")
        await processar_comando(chat_id, cmd_pendente, query.message, context)
    else:
        await query.edit_message_text(f"Projeto alterado para {label}")
        await _enviar_diff(query.message, projeto_path(chat_id), label)


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
        res = await rodar_async(cmd, cwd=cwd)
        await msg.reply_text(res["stdout"] or res["stderr"] or "(sem saída)")

    elif texto.startswith("/gitpush"):
        msg_commit = texto.split(" ", 1)[1].strip() if " " in texto else ""

        status = await rodar_async("git status --short", cwd=cwd)
        if not status["stdout"]:
            await msg.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
            return

        await _enviar_diff(msg, cwd, label)

        if not msg_commit:
            aguarde = await msg.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
            _, msg_commit = await _gerar_commit_ia(cwd)
            try:
                await aguarde.delete()
            except Exception:
                pass
            if not msg_commit:
                await msg.reply_text("⚠️ Não consegui gerar mensagem de commit.")
                return

        push_pendente[chat_id] = {"cwd": cwd, "msg_commit": msg_commit}
        teclado = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirmar Push", callback_data="push:sim"),
                InlineKeyboardButton("❌ Cancelar", callback_data="push:nao"),
            ]
        ])
        await msg.reply_text(
            f"💡 <b>Commit:</b>\n<code>{html.escape(msg_commit)}</code>\n\nConfirma o push?",
            parse_mode="HTML",
            reply_markup=teclado,
        )

    elif texto.startswith("/gitdiff"):
        await _enviar_diff(msg, cwd, label)
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Gerar resumo das alterações", callback_data="resumo_diff")]
        ])
        await msg.reply_text("Deseja um resumo do que foi modificado?", reply_markup=teclado)

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
    res = await rodar_async(cmd, cwd=projeto_path(update.effective_chat.id))
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
async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ia_apikey_pendente:
        del ia_apikey_pendente[chat_id]
        await update.message.reply_text("Configuração de IA cancelada.")
        return
    if chat_id in ia_modelo_pendente:
        del ia_modelo_pendente[chat_id]
        await update.message.reply_text("Configuração de IA cancelada.")
        return
    if chat_id in novo_projeto_pendente:
        del novo_projeto_pendente[chat_id]
        await update.message.reply_text("Criação de projeto cancelada.")
        return

    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)

    from lib.claude import claude_processos
    proc = claude_processos.get(cwd)

    from lib.claude import claude_locks
    lock = claude_locks.get(cwd)
    tem_fila = lock and lock.locked()

    if proc and proc.poll() is None:
        claude_cancelado.add(cwd)
        await update.message.reply_text(f"⏳ Cancelando... [{label}]")
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass
        claude_processos.pop(cwd, None)
        await update.message.reply_text(f"🛑 Tudo cancelado! [{label}]")
    elif tem_fila:
        claude_cancelado.add(cwd)
        await update.message.reply_text(f"🛑 Fila limpa! [{label}]")
    else:
        await update.message.reply_text(f"ℹ️ Nada para cancelar. [{label}]")


@autorizado
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🔄 Atualizando e reiniciando {BOT_NOME}...")
    subprocess.Popen(f"sleep 2 && cd {BOT_REPO_DIR} && git pull && systemctl --user restart {BOT_SERVICE}", shell=True)


@autorizado
async def cmd_restart_todos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = subprocess.run(
            ["systemctl", "--user", "list-units", "remotedev-*", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
        services = [line.split()[0] for line in res.stdout.strip().splitlines() if line.strip()]
    except Exception:
        services = [BOT_SERVICE]
    if not services:
        services = [BOT_SERVICE]
    # Coloca o bot atual por último para não matar o processo antes de reiniciar os outros
    if BOT_SERVICE in services:
        services.remove(BOT_SERVICE)
        services.append(BOT_SERVICE)
    nomes = ", ".join(s.replace("remotedev-", "").replace(".service", "") for s in services)
    await update.message.reply_text(f"🔄 Atualizando e reiniciando todos: {nomes}...")
    restart_cmd = " && ".join(f"systemctl --user restart {s}" for s in services)
    subprocess.Popen(f"sleep 2 && cd {BOT_REPO_DIR} && git pull && {restart_cmd}", shell=True)


# ══════════════════════════════════════════════════════════════════════
# MENSAGENS — TEXTO, ÁUDIO, FOTO
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def mensagem_livre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    if not texto:
        return

    chat_id = update.effective_chat.id

    # Fluxo: aguardando API key para análise com IA
    if chat_id in ia_apikey_pendente:
        apikey = texto.strip()
        if not apikey:
            await update.message.reply_text("API key inválida. Tente novamente ou /cancelar.")
            return
        await processar_apikey_ia(chat_id, apikey, update.message)
        return

    # Fluxo: aguardando nome do novo projeto
    if chat_id in novo_projeto_pendente:
        del novo_projeto_pendente[chat_id]
        nome = texto.lower().strip()
        if not validar_nome_projeto(nome):
            await update.message.reply_text(
                "Nome inválido. Use apenas letras minúsculas, números, hífens e underlines.\n"
                "Ex: <code>meu-app</code> ou <code>meu_app</code>",
                parse_mode="HTML",
            )
            return
        await criar_projeto(nome, chat_id, update.message)
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

        # Redimensiona e comprime para economizar tokens
        with Image.open(img_path) as img:
            if max(img.size) > 1024:
                img.thumbnail((1024, 1024))
            img.save(img_path, "JPEG", quality=80, optimize=True)

        if caption:
            prompt = f"Analise a imagem em {img_path} e responda: {caption}"
        else:
            prompt = f"Leia a imagem em {img_path}. Se for um erro ou bug, sugira a correção. Se for código, analise. Seja direto e objetivo, sem descrever a imagem."

        await enviar_para_claude(update, prompt)

        if os.path.exists(img_path):
            os.unlink(img_path)

    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao processar imagem: {e}")


@autorizado
async def mensagem_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.video_note
    if not video:
        return

    import shutil
    if not shutil.which("ffmpeg"):
        await update.message.reply_text("⚠️ ffmpeg não instalado. Rode ./bot.sh install para habilitar vídeos.")
        return

    if not await exigir_projeto(update):
        return

    caption = update.message.caption or ""
    await update.message.reply_text("🎬 Processando vídeo...")

    tmp_dir = tempfile.mkdtemp(prefix="remotedev_video_")
    try:
        # Baixar vídeo
        file = await video.get_file()
        video_path = os.path.join(tmp_dir, "video.mp4")
        await file.download_to_drive(video_path)

        partes_prompt = []

        # Extrair frames (1 a cada 3 segundos, max 5 frames)
        frames_dir = os.path.join(tmp_dir, "frames")
        os.makedirs(frames_dir)
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-vf", "fps=1/3", "-frames:v", "5", f"{frames_dir}/frame_%02d.jpg"],
            capture_output=True, timeout=30,
        )
        frames = sorted([os.path.join(frames_dir, f) for f in os.listdir(frames_dir) if f.endswith(".jpg")])
        if frames:
            paths_str = ", ".join(frames)
            partes_prompt.append(f"Frames do vídeo: {paths_str}")

        # Extrair e transcrever áudio (se tiver OpenAI key)
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            audio_path = os.path.join(tmp_dir, "audio.ogg")
            result = subprocess.run(
                ["ffmpeg", "-i", video_path, "-vn", "-acodec", "libopus", audio_path],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and os.path.getsize(audio_path) > 0:
                try:
                    transcricao = await transcrever_audio(audio_path)
                    if transcricao and transcricao.strip():
                        partes_prompt.append(f"Áudio do vídeo: {transcricao}")
                        await update.message.reply_text(f"📝 Áudio: {transcricao}")
                except Exception:
                    pass

        if not partes_prompt:
            await update.message.reply_text("⚠️ Não consegui extrair conteúdo do vídeo.")
            return

        # Montar prompt
        contexto = "\n".join(partes_prompt)
        if caption:
            prompt = f"{contexto}\n\nPergunta do usuário: {caption}"
        else:
            prompt = f"{contexto}\n\nAnalise o conteúdo do vídeo. Seja direto e objetivo."

        await enviar_para_claude(update, prompt)

    except subprocess.TimeoutExpired:
        await update.message.reply_text("⚠️ Vídeo muito longo ou pesado para processar.")
    except Exception as e:
        erro = str(e)
        if "file is too big" in erro.lower() or "file_too_big" in erro.lower():
            await update.message.reply_text("⚠️ Vídeo muito grande. Limite do Telegram: 20MB.")
        elif "wrong file_id" in erro.lower() or "invalid" in erro.lower():
            await update.message.reply_text("⚠️ Não consegui baixar o vídeo. Tente enviar novamente.")
        else:
            await update.message.reply_text(f"❌ Erro ao processar vídeo: {erro}")
    finally:
        import shutil as sh
        sh.rmtree(tmp_dir, ignore_errors=True)


@autorizado
async def mensagem_documento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return
    if not await exigir_projeto(update):
        return

    caption = update.message.caption or ""

    try:
        file = await doc.get_file()
        nome_original = doc.file_name or "documento"
        sufixo = os.path.splitext(nome_original)[1] or ""
        nome_tmp = f"telegram_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{nome_original}"
        file_path = os.path.join(tempfile.gettempdir(), nome_tmp)
        await file.download_to_drive(file_path)

        if caption:
            prompt = f"Leia o arquivo {file_path} (nome original: {nome_original}) e responda: {caption}"
        else:
            prompt = f"Leia o arquivo {file_path} (nome original: {nome_original}). Analise o conteúdo e dê um resumo objetivo."

        await enviar_para_claude(update, prompt)

        if os.path.exists(file_path):
            os.unlink(file_path)

    except Exception as e:
        erro = str(e)
        if "file is too big" in erro.lower() or "file_too_big" in erro.lower():
            await update.message.reply_text("⚠️ Arquivo muito grande. Limite do Telegram: 20MB.")
        else:
            await update.message.reply_text(f"❌ Erro ao processar documento: {erro}")


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
    app.add_handler(CallbackQueryHandler(callback_novo_projeto, pattern=r"^novo_projeto$"))
    app.add_handler(CallbackQueryHandler(callback_uso_projeto, pattern=r"^uso_projeto:"))
    app.add_handler(CallbackQueryHandler(callback_github_novo, pattern=r"^github_novo:"))
    app.add_handler(CallbackQueryHandler(callback_ia_analise, pattern=r"^ia_analise:"))
    app.add_handler(CallbackQueryHandler(callback_ia_provider, pattern=r"^ia_provider:"))
    app.add_handler(CallbackQueryHandler(callback_ia_modelo, pattern=r"^ia_modelo:"))
    app.add_handler(CallbackQueryHandler(callback_voltar_projeto, pattern=r"^voltar_projeto$"))
    app.add_handler(CallbackQueryHandler(callback_excluir_projeto, pattern=r"^excluir_projeto$"))
    app.add_handler(CallbackQueryHandler(callback_confirmar_exclusao, pattern=r"^confirmar_exclusao:"))
    app.add_handler(CallbackQueryHandler(callback_excluir, pattern=r"^excluir:sim:"))
    app.add_handler(CallbackQueryHandler(callback_projeto, pattern=r"^projeto:"))
    app.add_handler(CallbackQueryHandler(callback_branch, pattern=r"^branch:"))
    app.add_handler(CallbackQueryHandler(callback_push, pattern=r"^push:"))
    app.add_handler(CallbackQueryHandler(callback_reset, pattern=r"^reset:"))
    app.add_handler(CallbackQueryHandler(callback_resumo_diff, pattern=r"^resumo_diff$"))

    # Comandos
    app.add_handler(CommandHandler("start", cmd_menu))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ping_pc", cmd_ping_pc))
    app.add_handler(CommandHandler("bash", cmd_bash))
    app.add_handler(CommandHandler("limpar_conversa", cmd_new_session))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("gitdiff", cmd_diff))
    app.add_handler(CommandHandler("gitreset", cmd_gitreset))
    app.add_handler(CommandHandler("gitbranch", cmd_gitbranch))
    app.add_handler(CommandHandler("gitpush", cmd_push))
    app.add_handler(CommandHandler("restart_bot", cmd_restart))
    app.add_handler(CommandHandler("restart_todos", cmd_restart_todos))

    # Comando desconhecido
    @autorizado
    async def cmd_desconhecido(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cmd = update.message.text.split()[0]
        await update.message.reply_text(f"Comando {cmd} não existe. Use /menu pra ver os disponíveis.")
    app.add_handler(MessageHandler(filters.COMMAND, cmd_desconhecido))

    # Mensagens
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagem_livre))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, mensagem_audio))
    app.add_handler(MessageHandler(filters.PHOTO, mensagem_foto))

    # Vídeo → extrair frames + áudio → Claude
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, mensagem_video))

    # Documentos (PDF, CSV, Excel, etc.) → Claude
    app.add_handler(MessageHandler(filters.Document.ALL, mensagem_documento))

    async def post_init(application):
        commands = []
        for line in BOTFATHER_COMMANDS.strip().split("\n"):
            if " - " in line:
                cmd, desc = line.split(" - ", 1)
                commands.append(BotCommand(cmd.strip(), desc.strip()))
        await application.bot.set_my_commands(commands)

        # Se já tem projeto ativo restaurado do disco, apenas informa
        proj_restaurado = projeto_ativo(CHAT_ID)
        if proj_restaurado and proj_restaurado in descobrir_projetos(WORKSPACE):
            label = projeto_label(CHAT_ID)
            await atualizar_nome_bot(application.bot, CHAT_ID)
            await application.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🟢 {BOT_NOME} iniciado!\n📂 Projeto restaurado: *{label}*",
                parse_mode="Markdown",
            )
        else:
            await atualizar_nome_bot(application.bot, CHAT_ID)
            projetos_atuais = descobrir_projetos(WORKSPACE)
            teclado = [
                [InlineKeyboardButton(cfg['nome'], callback_data=f"projeto:{key}")]
                for key, cfg in projetos_atuais.items()
            ]
            teclado.append([
                InlineKeyboardButton("➕ Novo Projeto", callback_data="novo_projeto"),
                InlineKeyboardButton("🗑 Excluir Projeto", callback_data="excluir_projeto"),
            ])
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
