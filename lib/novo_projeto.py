import os
import re
import html
import asyncio
import socket

from telegram import Update
from telegram.ext import ContextTypes

from lib.config import PROJETOS, WORKSPACE, descobrir_projetos
from lib.utils import novo_projeto_pendente, estado, autorizado

# PATH expandido para subprocessos (systemd não carrega .bashrc)
_ENV = {**os.environ, "PATH": os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")}

# Processos de dev server + tunnel por projeto (para cleanup)
_tunnel_procs = {}  # nome → {"dev": Process, "tunnel": Process}

_PORTA_INICIAL = 5000


def _proxima_porta_livre() -> int:
    """Encontra a próxima porta livre a partir de _PORTA_INICIAL."""
    # Portas já em uso pelos tunnels ativos
    portas_usadas = {info.get("porta") for info in _tunnel_procs.values() if "porta" in info}
    porta = _PORTA_INICIAL
    while porta < 6000:
        if porta in portas_usadas:
            porta += 1
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", porta))
                return porta
            except OSError:
                porta += 1
    raise RuntimeError("Nenhuma porta livre entre 5000-5999")


@autorizado
async def callback_novo_projeto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    novo_projeto_pendente[chat_id] = True
    await query.edit_message_text(
        "Digite o nome do projeto (lowercase, sem espaços):\n"
        "Ex: <code>meu-app</code>\n\n"
        "Ou /cancelar para desistir.",
        parse_mode="HTML",
    )


def validar_nome_projeto(nome: str) -> bool:
    return bool(re.match(r'^[a-z][a-z0-9_-]*$', nome))



async def criar_projeto(nome: str, chat_id: int, msg):
    """Cria projeto Next.js completo com GitHub repo."""
    projeto_dir = os.path.join(WORKSPACE, nome)

    if os.path.exists(projeto_dir):
        await msg.reply_text(f"Já existe um diretório <code>{nome}</code> no workspace.", parse_mode="HTML")
        return

    await msg.reply_text(f"⏳ Criando projeto <b>{nome}</b>...\nIsso pode levar alguns minutos.", parse_mode="HTML")

    passos = [
        ("Criando Next.js + TypeScript + Tailwind...", [
            "pnpm", "create", "next-app@latest", projeto_dir,
            "--typescript", "--tailwind", "--app", "--use-pnpm",
            "--eslint", "--no-src-dir", "--no-import-alias", "--turbopack",
            "--no-react-compiler", "--no-agents-md", "--yes",
        ]),
    ]

    for descricao, cmd in passos:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            erro = stderr.decode().strip() or stdout.decode().strip()
            await msg.reply_text(f"Erro ao criar projeto:\n<pre>{html.escape(erro[:2000])}</pre>", parse_mode="HTML")
            return

    # Verificar se o diretório foi criado
    if not os.path.exists(projeto_dir):
        await msg.reply_text(
            f"❌ Erro: diretório <code>{nome}</code> não foi criado pelo create-next-app.\n"
            "Pode ser que o comando tenha pedido input interativo não suportado.",
            parse_mode="HTML",
        )
        return

    # Instalar dependências extras
    await msg.reply_text("📦 Instalando Zod + Biome + shadcn...")
    extras = [
        (["pnpm", "add", "zod"], projeto_dir),
        (["pnpm", "add", "-D", "@biomejs/biome"], projeto_dir),
        (["pnpm", "dlx", "shadcn@latest", "init", "-y", "--defaults"], projeto_dir),
    ]
    for cmd, cwd in extras:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            erro = stderr.decode().strip() or stdout.decode().strip()
            await msg.reply_text(
                f"Aviso ao rodar <code>{' '.join(cmd)}</code>:\n<pre>{html.escape(erro[:1500])}</pre>",
                parse_mode="HTML",
            )

    # Gerar CLAUDE.md
    claude_md = f"""# {nome}

Projeto Next.js (App Router) com TypeScript, Tailwind CSS, shadcn/ui e Zod.

## Stack

- **Framework:** Next.js (App Router) + TypeScript
- **UI:** Tailwind CSS + shadcn/ui
- **Validação:** Zod
- **Linter/Formatter:** Biome
- **Pacotes:** pnpm

## Comandos

```bash
pnpm dev          # servidor de desenvolvimento
pnpm build        # build de produção
pnpm lint         # linter (Next.js)
pnpm biome check  # linter + formatter (Biome)
```

## Convenções

- Código e comentários em português
- Componentes em `components/`
- Componentes de UI (shadcn) em `components/ui/`
- Páginas no App Router em `app/`
"""
    with open(os.path.join(projeto_dir, "CLAUDE.md"), "w") as f:
        f.write(claude_md)

    # Configurar Biome
    biome_config = """{
  "$schema": "https://biomejs.dev/schemas/2.0.0/schema.json",
  "formatter": {
    "indentStyle": "space",
    "indentWidth": 2
  },
  "linter": {
    "enabled": true
  }
}
"""
    with open(os.path.join(projeto_dir, "biome.json"), "w") as f:
        f.write(biome_config)

    # Git init + primeiro commit
    await msg.reply_text("🔧 Configurando Git + GitHub...")
    git_cmds = [
        ["git", "add", "."],
        ["git", "commit", "-m", "feat: init projeto com Next.js + TS + Tailwind + shadcn + Zod + Biome"],
    ]
    for cmd in git_cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=projeto_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
        await proc.communicate()

    # Criar repo no GitHub e push
    proc = await asyncio.create_subprocess_exec(
        "gh", "repo", "create", nome, "--public", "--source", projeto_dir, "--push",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_ENV,
    )
    stdout, stderr = await proc.communicate()
    gh_output = stdout.decode().strip()
    gh_erro = stderr.decode().strip()

    if proc.returncode != 0:
        await msg.reply_text(
            f"Projeto criado localmente mas erro no GitHub:\n<pre>{html.escape(gh_erro[:1500])}</pre>\n\n"
            f"Você pode criar manualmente com:\n<code>gh repo create {nome} --public --source {projeto_dir} --push</code>",
            parse_mode="HTML",
        )
    else:
        repo_url = gh_output if gh_output.startswith("http") else f"https://github.com/{gh_output}"
        await msg.reply_text(
            f"✅ Projeto <b>{nome}</b> criado!\n\n"
            f"📁 <code>{projeto_dir}</code>\n"
            f"🔗 {repo_url}\n\n"
            f"Stack: Next.js + TS + Tailwind + shadcn/ui + Zod + Biome\n\n"
            f"Use /projeto para selecioná-lo.",
            parse_mode="HTML",
        )

    # Iniciar dev server + tunnel público
    porta = _proxima_porta_livre()
    await msg.reply_text(f"🌐 Iniciando servidor dev (porta {porta}) + tunnel público...")
    try:
        dev_proc = await asyncio.create_subprocess_exec(
            "pnpm", "dev", "--port", str(porta),
            cwd=projeto_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ENV,
        )
        await asyncio.sleep(4)  # esperar o dev server subir

        tunnel_proc = await asyncio.create_subprocess_exec(
            "npx", "localtunnel", "--port", str(porta),
            cwd=projeto_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
        # localtunnel imprime a URL na primeira linha do stdout
        url_line = await asyncio.wait_for(tunnel_proc.stdout.readline(), timeout=15)
        tunnel_url = url_line.decode().strip()
        # Extrair URL — output é "your url is: https://xyz.loca.lt"
        if "url is:" in tunnel_url.lower():
            tunnel_url = tunnel_url.split("url is:")[-1].strip()

        _tunnel_procs[nome] = {"dev": dev_proc, "tunnel": tunnel_proc, "porta": porta}
        await msg.reply_text(
            f"🌐 <b>URL pública:</b> {tunnel_url}\n"
            f"🏠 <b>URL local:</b> http://localhost:{porta}\n\n"
            f"Tunnel ativo enquanto o bot estiver rodando.",
            parse_mode="HTML",
        )
    except (asyncio.TimeoutError, Exception) as e:
        await msg.reply_text(f"⚠️ Projeto criado, mas erro ao iniciar tunnel:\n<pre>{html.escape(str(e)[:500])}</pre>", parse_mode="HTML")

    # Atualizar lista de projetos e trocar para o novo
    PROJETOS.clear()
    PROJETOS.update(descobrir_projetos(WORKSPACE))
    if nome in PROJETOS:
        estado[chat_id] = nome
        await msg.reply_text(f"📂 Projeto ativo alterado para <b>{nome}</b>.", parse_mode="HTML")
