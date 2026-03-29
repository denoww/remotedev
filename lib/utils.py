import os
import asyncio
import subprocess
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from lib.config import (
    PROJETOS, PROJETO_PADRAO, CHAT_ID, DEFAULT_TIMEOUT,
    MAX_STDOUT, TELEGRAM_MSG_LIMIT, BOT_NOME,
)

# Estado global
estado = {}
pendente = {}  # chat_id → mensagem original pendente após escolha de projeto
push_pendente = {}  # chat_id → {cwd, msg_commit} aguardando confirmação
reset_pendente = {}  # chat_id → {cwd, label} aguardando confirmação
novo_projeto_pendente = {}  # chat_id → True quando aguardando nome do novo projeto
ia_apikey_pendente = {}  # chat_id → {nome, provider} aguardando API key para análise IA
ia_modelo_pendente = {}  # chat_id → {nome, provider, apikey, modelos} aguardando escolha de modelo IA


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


def resumo_git(cwd: str) -> str:
    """Retorna resumo curto do estado git do projeto, ou string vazia se limpo."""
    res = subprocess.run(
        ["git", "status", "--short"],
        cwd=cwd, capture_output=True, text=True, timeout=5,
    )
    linhas = res.stdout.strip()
    if not linhas:
        return ""
    lista = linhas.split("\n")
    if len(lista) > 15:
        lista = lista[:15] + [f"… e mais {len(lista) - 15} arquivo(s)"]
    return "\n".join(lista)


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
        return f"{cfg['nome']} ({branch})"
    return f"{cfg['nome']}"




async def atualizar_nome_bot(bot, chat_id: int):
    """Atualiza o nome de exibição do bot para refletir o projeto ativo."""
    cfg = projeto_config(chat_id)
    if cfg:
        path = cfg["path"]
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            branch = ""
        nome = f"{cfg['nome']} ({branch})" if branch else cfg["nome"]
    else:
        nome = f"remotedev ({BOT_NOME})"
    try:
        await bot.set_my_name(name=nome[:64])
    except Exception:
        pass  # ignora erros silenciosamente (rate limit, etc)


async def exigir_projeto(update: Update) -> bool:
    """Retorna True se tem projeto selecionado. Se não, pede para escolher."""
    chat_id = update.effective_chat.id
    if projeto_config(chat_id) is not None:
        return True
    if update.message and update.message.text:
        pendente[chat_id] = update.message.text
    teclado = [
        [InlineKeyboardButton(cfg['nome'], callback_data=f"projeto:{key}")]
        for key, cfg in PROJETOS.items()
    ]
    teclado.append([
        InlineKeyboardButton("➕ Novo Projeto", callback_data="novo_projeto"),
        InlineKeyboardButton("🗑 Excluir Projeto", callback_data="excluir_projeto"),
    ])
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
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
            env={**os.environ, "TERM": "dumb"},
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        truncated = False

        if len(stdout) > MAX_STDOUT:
            stdout = stdout[:MAX_STDOUT] + "\n\n… (truncado)"
            truncated = True

        return {"stdout": stdout, "stderr": stderr, "code": result.returncode, "truncated": truncated}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"⏰ Timeout após {timeout}s", "code": -1, "truncated": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "code": -1, "truncated": False}


async def rodar_async(cmd: str, cwd: str = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Versão async de rodar() — não bloqueia o event loop."""
    return await asyncio.to_thread(rodar, cmd, cwd, timeout)


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
    msg = update.message or getattr(update.callback_query, 'message', None)
    if not msg:
        return
    pedacos = [texto[i:i + TELEGRAM_MSG_LIMIT] for i in range(0, len(texto), TELEGRAM_MSG_LIMIT)]
    for pedaco in pedacos:
        try:
            await msg.reply_text(pedaco, parse_mode="HTML")
        except Exception:
            await msg.reply_text(pedaco)
