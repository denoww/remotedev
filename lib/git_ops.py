import os
import html
import shlex
import asyncio
import subprocess

from lib.config import MAX_DIFF
from lib.utils import rodar, rodar_async, projeto_path, projeto_label, enviar_resultado, exigir_projeto, autorizado, push_pendente, reset_pendente
from lib.hooks import pos_push
from lib.claude import rodar_claude

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes


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
    # Se rejeitado por non-fast-forward, faz pull --rebase e tenta de novo
    if res["code"] != 0 and "non-fast-forward" in (res["stderr"] or ""):
        pull = rodar("git pull --rebase", cwd=cwd, timeout=timeout)
        if pull["code"] == 0:
            res = rodar("git push", cwd=cwd, timeout=timeout)
    return res


def _obter_diff_texto(cwd):
    """Retorna o diff completo do projeto (staged + unstaged + untracked)."""
    diff_out = rodar("git diff", cwd=cwd)
    diff_cached = rodar("git diff --cached", cwd=cwd)
    diff_texto = (diff_out["stdout"] or "") + "\n" + (diff_cached["stdout"] or "")
    diff_texto = diff_texto.strip()

    if not diff_texto:
        status = rodar("git status --short", cwd=cwd)
        diff_texto = status["stdout"] or ""

    if len(diff_texto) > MAX_DIFF:
        diff_texto = diff_texto[:MAX_DIFF] + "\n... (diff truncado)"

    return diff_texto


async def _gerar_commit_ia(cwd):
    """Gera mensagem de commit via Claude AI. Retorna (resumo, msg_commit)."""
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


async def _enviar_diff(msg, cwd, label):
    """Mostra alterações pendentes do git."""
    status = await rodar_async("git status --short", cwd=cwd)
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

    diff_stat = await rodar_async("git diff --stat", cwd=cwd)
    texto = f"📊 [{label}] {', '.join(resumo)}\n\n"
    texto += f"<pre>{html.escape(status['stdout'])}</pre>"
    if diff_stat["stdout"]:
        texto += f"\n\n<pre>{html.escape(diff_stat['stdout'])}</pre>"

    try:
        await msg.reply_text(texto, parse_mode="HTML")
    except Exception:
        await msg.reply_text(texto)


# ══════════════════════════════════════════════════════════════════════
# COMANDOS GIT
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra diff e oferece gerar resumo via IA."""
    if not await exigir_projeto(update):
        return
    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    status = await rodar_async("git status --short", cwd=cwd)
    if not status["stdout"]:
        await update.message.reply_text(f"✅ [{label}] Nenhuma alteração pendente.")
        return

    await _enviar_diff(update.message, cwd, label)

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Gerar resumo das alterações", callback_data="resumo_diff")]
    ])
    await update.message.reply_text(
        "Deseja um resumo do que foi modificado?",
        reply_markup=teclado,
    )


@autorizado
async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra diff e commit, pede confirmação antes de fazer push."""
    if not await exigir_projeto(update):
        return

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)
    msg_commit = " ".join(context.args).strip() if context.args else ""

    status = await rodar_async("git status --short", cwd=cwd)
    if not status["stdout"]:
        await update.message.reply_text("⚠️ Nenhuma alteração encontrada para commitar.")
        return

    # Mostra diff
    await _enviar_diff(update.message, cwd, label)

    # Gera ou usa mensagem de commit
    if not msg_commit:
        aguarde = await update.message.reply_text(f"🤖 [{label}] Gerando mensagem de commit...")
        _, msg_commit = await _gerar_commit_ia(cwd)
        try:
            await aguarde.delete()
        except Exception:
            pass
        if not msg_commit:
            await update.message.reply_text("⚠️ Não consegui gerar mensagem de commit.")
            return

    # Salva dados e pede confirmação
    push_pendente[chat_id] = {"cwd": cwd, "msg_commit": msg_commit}
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmar Push", callback_data="push:sim"),
            InlineKeyboardButton("❌ Cancelar", callback_data="push:nao"),
        ]
    ])
    await update.message.reply_text(
        f"💡 <b>Commit:</b>\n<code>{html.escape(msg_commit)}</code>\n\nConfirma o push?",
        parse_mode="HTML",
        reply_markup=teclado,
    )


@autorizado
async def callback_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa ou cancela o push pendente."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    acao = query.data.replace("push:", "")

    dados = push_pendente.pop(chat_id, None)
    if not dados:
        await query.edit_message_text("⚠️ Nenhum push pendente.")
        return

    if acao != "sim":
        await query.edit_message_text("❌ Push cancelado.")
        return

    cwd = dados["cwd"]
    msg_commit = dados["msg_commit"]
    await query.edit_message_text(f"⏳ Push: {msg_commit}")

    res = await rodar_async("git add -A", cwd=cwd)
    if res["code"] != 0:
        await enviar_resultado(update, res, "git add -A")
        return

    cmd_commit = f'git commit -m {shlex.quote(msg_commit)}'
    res = await rodar_async(cmd_commit, cwd=cwd)
    if res["code"] != 0:
        if "nothing to commit" in (res["stdout"] + res["stderr"]):
            await query.message.reply_text("⚠️ Nada para commitar.")
            return
        await enviar_resultado(update, res, cmd_commit)
        return

    res = await asyncio.to_thread(git_push, cwd)
    await enviar_resultado(update, res, f"git push ({msg_commit})")
    await pos_push(update, cwd, res)


@autorizado
async def cmd_gitbranch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Troca de branch (cria se não existir) e faz git pull."""
    if not await exigir_projeto(update):
        return

    branch = " ".join(context.args).strip() if context.args else ""
    if not branch:
        cwd = projeto_path(update.effective_chat.id)
        res = await rodar_async("git branch", cwd=cwd)
        branches = []
        atual = ""
        for line in res["stdout"].split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("* "):
                atual = line[2:]
                branches.append(atual)
            else:
                branches.append(line)
        teclado = []
        for b in branches:
            marcador = " ✅" if b == atual else ""
            teclado.append([InlineKeyboardButton(f"{b}{marcador}", callback_data=f"branch:{b}")])
        await update.message.reply_text(
            "Selecione a branch:",
            reply_markup=InlineKeyboardMarkup(teclado),
        )
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)

    res = await rodar_async(f"git checkout {branch}", cwd=cwd)
    if res["code"] != 0:
        res = await rodar_async(f"git checkout -b {branch}", cwd=cwd)
        if res["code"] != 0:
            await enviar_resultado(update, res, f"git checkout -b {branch}")
            return
        await update.message.reply_text(f"🌿 [{label}] Branch <code>{branch}</code> criada!", parse_mode="HTML")
    else:
        await update.message.reply_text(f"🔀 [{label}] Branch: <code>{branch}</code>", parse_mode="HTML")

    res_pull = await rodar_async("git pull", cwd=cwd, timeout=60)
    if res_pull["code"] == 0 and res_pull["stdout"]:
        await update.message.reply_text(f"⬇️ {res_pull['stdout']}")


@autorizado
async def cmd_gitreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra status e pede confirmação antes de descartar alterações."""
    if not await exigir_projeto(update):
        return

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    status = await rodar_async("git status --short", cwd=cwd)
    if not status["stdout"]:
        await update.message.reply_text("⚠️ Nenhuma alteração para descartar.")
        return

    reset_pendente[chat_id] = {"cwd": cwd, "label": label}
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmar Reset", callback_data="reset:sim"),
            InlineKeyboardButton("❌ Cancelar", callback_data="reset:nao"),
        ]
    ])
    await update.message.reply_text(
        f"⚠️ <b>Alterações que serão descartadas:</b>\n<pre>{html.escape(status['stdout'])}</pre>\n\nConfirma o reset?",
        parse_mode="HTML",
        reply_markup=teclado,
    )


@autorizado
async def callback_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa ou cancela o reset pendente."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    acao = query.data.replace("reset:", "")

    dados = reset_pendente.pop(chat_id, None)
    if not dados:
        await query.edit_message_text("⚠️ Nenhum reset pendente.")
        return

    if acao != "sim":
        await query.edit_message_text("❌ Reset cancelado.")
        return

    cwd = dados["cwd"]
    label = dados["label"]
    await rodar_async("git checkout .", cwd=cwd)
    await rodar_async("git clean -fd", cwd=cwd)
    await query.edit_message_text(f"🗑️ Todas as alterações descartadas! [{label}]")


async def callback_resumo_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gera resumo das alterações via IA quando o usuário pede."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)

    await query.edit_message_text(f"🤖 [{label}] Gerando resumo...")
    resumo, msg_commit = await _gerar_commit_ia(cwd)

    texto = ""
    if resumo:
        texto += f"📝 <b>Resumo:</b> {html.escape(resumo)}\n\n"
    texto += f"💡 <b>Commit:</b>\n<code>{html.escape(msg_commit or '(sem sugestão)')}</code>"

    try:
        await query.edit_message_text(texto, parse_mode="HTML")
    except Exception:
        await query.edit_message_text(texto)


async def callback_branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    branch = query.data.replace("branch:", "")
    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)

    res = await rodar_async(f"git checkout {branch}", cwd=cwd)
    if res["code"] != 0:
        erro = res["stderr"] or res["stdout"] or "erro desconhecido"
        await query.edit_message_text(f"❌ Erro ao trocar para {branch}:\n{erro}")
        return

    label = projeto_label(chat_id)
    await query.edit_message_text(f"🔀 {label}")

    res_pull = await rodar_async("git pull", cwd=cwd, timeout=60)
    if res_pull["code"] == 0 and res_pull["stdout"]:
        await query.message.reply_text(f"⬇️ {res_pull['stdout']}")
