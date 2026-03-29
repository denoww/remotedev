import os
import stat
import shutil

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from lib.config import PROJETOS, WORKSPACE, BOT_REPO_DIR, descobrir_projetos
from lib.utils import estado, autorizado, atualizar_nome_bot
from lib.novo_projeto import _tunnel_procs


@autorizado
async def callback_excluir_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra lista de projetos para excluir."""
    query = update.callback_query
    await query.answer()

    # Não permitir excluir o próprio remotedev
    projetos_excluiveis = {
        k: v for k, v in PROJETOS.items()
        if os.path.realpath(v["path"]) != os.path.realpath(BOT_REPO_DIR)
    }

    if not projetos_excluiveis:
        await query.edit_message_text("Nenhum projeto disponível para exclusão.")
        return

    teclado = [
        [InlineKeyboardButton(f"🗑 {cfg['nome']}", callback_data=f"confirmar_exclusao:{key}")]
        for key, cfg in projetos_excluiveis.items()
    ]
    teclado.append([InlineKeyboardButton("↩️ Voltar", callback_data="voltar_projeto")])

    await query.edit_message_text(
        "⚠️ Selecione o projeto para <b>excluir localmente</b>:\n"
        "(o repositório no GitHub <b>NÃO</b> será afetado)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(teclado),
    )


@autorizado
async def callback_confirmar_exclusao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pede confirmação antes de excluir."""
    query = update.callback_query
    await query.answer()

    nome = query.data.split(":", 1)[1]
    if nome not in PROJETOS:
        await query.edit_message_text("Projeto não encontrado.")
        return

    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Sim, excluir", callback_data=f"excluir:sim:{nome}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="voltar_projeto"),
        ]
    ])
    await query.edit_message_text(
        f"🗑 Tem certeza que deseja excluir <b>{nome}</b>?\n\n"
        f"📁 <code>{PROJETOS[nome]['path']}</code>\n\n"
        "⚠️ O diretório local será apagado permanentemente.\n"
        "O repositório no GitHub <b>NÃO</b> será afetado.",
        parse_mode="HTML",
        reply_markup=teclado,
    )


@autorizado
async def callback_excluir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa a exclusão do projeto."""
    query = update.callback_query
    await query.answer()

    _, resposta, nome = query.data.split(":", 2)

    if nome not in PROJETOS:
        await query.edit_message_text("Projeto não encontrado.")
        return

    projeto_dir = PROJETOS[nome]["path"]

    # Matar processos de dev server + tunnel se houver
    procs = _tunnel_procs.pop(nome, None)
    if procs:
        for key in ("dev", "tunnel"):
            proc = procs.get(key)
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    # Excluir diretório (force: corrige permissões de pastas como .turbo, node_modules)
    def _force_remove(func, path, exc_info):
        os.chmod(path, stat.S_IRWXU)
        func(path)

    try:
        shutil.rmtree(projeto_dir, onexc=_force_remove)
    except Exception as e:
        await query.edit_message_text(
            f"❌ Erro ao excluir <b>{nome}</b>:\n<pre>{e}</pre>",
            parse_mode="HTML",
        )
        return

    # Limpar estado: remover projeto ativo de quem estava usando
    for chat_id, proj in list(estado.items()):
        if proj == nome:
            del estado[chat_id]
            await atualizar_nome_bot(context.bot, chat_id)

    # Atualizar lista de projetos
    PROJETOS.clear()
    PROJETOS.update(descobrir_projetos(WORKSPACE))

    await query.edit_message_text(
        f"✅ Projeto <b>{nome}</b> excluído localmente.\n"
        f"O repositório no GitHub não foi afetado.",
        parse_mode="HTML",
    )
