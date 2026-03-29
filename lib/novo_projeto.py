import os
import re
import html
import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from lib.config import PROJETOS, WORKSPACE, descobrir_projetos
from lib.utils import novo_projeto_pendente, autorizado


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
    return bool(re.match(r'^[a-z][a-z0-9-]*$', nome))


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
        ]),
    ]

    for descricao, cmd in passos:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            erro = stderr.decode().strip() or stdout.decode().strip()
            await msg.reply_text(f"Erro ao criar projeto:\n<pre>{html.escape(erro[:2000])}</pre>", parse_mode="HTML")
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
        )
        await proc.communicate()

    # Criar repo no GitHub e push
    proc = await asyncio.create_subprocess_exec(
        "gh", "repo", "create", nome, "--public", "--source", projeto_dir, "--push",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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

    # Atualizar lista de projetos
    PROJETOS.clear()
    PROJETOS.update(descobrir_projetos(WORKSPACE))
