import os
import html
import shlex
import asyncio
import subprocess

from lib.config import MAX_DIFF
from lib.utils import rodar, projeto_path, projeto_label, enviar_resultado, exigir_projeto, autorizado
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


# ══════════════════════════════════════════════════════════════════════
# COMANDOS GIT
# ══════════════════════════════════════════════════════════════════════

@autorizado
async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    await _enviar_diff(update.message, cwd, label)

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
async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faz add, commit e push. Se não passar mensagem, gera via IA."""
    if not await exigir_projeto(update):
        return

    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)
    label = projeto_label(chat_id)
    msg_commit = " ".join(context.args).strip() if context.args else ""

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

    res = rodar("git add -A", cwd=cwd)
    if res["code"] != 0:
        await enviar_resultado(update, res, "git add -A")
        return

    cmd_commit = f'git commit -m {shlex.quote(msg_commit)}'
    res = rodar(cmd_commit, cwd=cwd)
    if res["code"] != 0:
        if "nothing to commit" in (res["stdout"] + res["stderr"]):
            await update.message.reply_text("⚠️ Nada para commitar.")
            return
        await enviar_resultado(update, res, cmd_commit)
        return

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
        cwd = projeto_path(update.effective_chat.id)
        res = rodar("git branch", cwd=cwd)
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

    res = rodar(f"git checkout {branch}", cwd=cwd)
    if res["code"] != 0:
        res = rodar(f"git checkout -b {branch}", cwd=cwd)
        if res["code"] != 0:
            await enviar_resultado(update, res, f"git checkout -b {branch}")
            return
        await update.message.reply_text(f"🌿 [{label}] Branch <code>{branch}</code> criada!", parse_mode="HTML")
    else:
        await update.message.reply_text(f"🔀 [{label}] Branch: <code>{branch}</code>", parse_mode="HTML")

    res_pull = rodar("git pull", cwd=cwd, timeout=60)
    if res_pull["code"] == 0 and res_pull["stdout"]:
        await update.message.reply_text(f"⬇️ {res_pull['stdout']}")


@autorizado
async def cmd_gitreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpa todas as alterações pendentes."""
    if not await exigir_projeto(update):
        return

    cwd = projeto_path(update.effective_chat.id)
    label = projeto_label(update.effective_chat.id)

    rodar("git checkout .", cwd=cwd)
    rodar("git clean -fd", cwd=cwd)
    await update.message.reply_text(f"🗑️ Todas as alterações descartadas! [{label}]")


async def callback_branch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    branch = query.data.replace("branch:", "")
    chat_id = update.effective_chat.id
    cwd = projeto_path(chat_id)

    res = rodar(f"git checkout {branch}", cwd=cwd)
    if res["code"] != 0:
        erro = res["stderr"] or res["stdout"] or "erro desconhecido"
        await query.edit_message_text(f"❌ Erro ao trocar para {branch}:\n{erro}")
        return

    label = projeto_label(chat_id)
    await query.edit_message_text(f"🔀 {label}")

    res_pull = rodar("git pull", cwd=cwd, timeout=60)
    if res_pull["code"] == 0 and res_pull["stdout"]:
        await query.message.reply_text(f"⬇️ {res_pull['stdout']}")
