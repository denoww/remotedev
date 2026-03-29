import os
import re
import html
import json
import shutil
import asyncio
import socket

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from lib.config import PROJETOS, WORKSPACE, descobrir_projetos
from lib.utils import novo_projeto_pendente, estado, autorizado

# PATH expandido para subprocessos (systemd não carrega .bashrc)
_ENV = {**os.environ, "PATH": os.path.expanduser("~/bin") + ":" + os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")}

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

    # Alocar porta para o dev server (nunca usar 3000)
    porta = _proxima_porta_livre()

    # Fixar porta do dev server no package.json
    pkg_path = os.path.join(projeto_dir, "package.json")
    with open(pkg_path) as f:
        pkg = json.load(f)
    pkg["scripts"]["dev"] = f"next dev --turbopack --port {porta}"
    with open(pkg_path, "w") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")

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
pnpm dev          # servidor de desenvolvimento (porta {porta})
pnpm build        # build de produção
pnpm lint         # linter (Next.js)
pnpm biome check  # linter + formatter (Biome)
```

**Porta do dev server: {porta}** — nunca use a porta 3000.

## Convenções

- Código e comentários em português
- Componentes em `components/`
- Componentes de UI (shadcn) em `components/ui/`
- Páginas no App Router em `app/`

## Ligar servidor e URL pública

Quando o usuário pedir para ligar o servidor, URL pública, ou testar o app:

**Execute os comandos abaixo EM UMA ÚNICA chamada bash, todos juntos:**

```bash
# Matar processos anteriores SOMENTE deste projeto (pela porta {porta})
pkill -f 'next dev.*--port {porta}' 2>/dev/null || true
pkill -f 'ngrok http.*--name {nome}' 2>/dev/null || true
sleep 1

# Iniciar dev server em background
nohup pnpm dev > /tmp/{nome}-dev.log 2>&1 &
sleep 4

# Verificar se o servidor subiu
curl -s -o /dev/null -w "%{{http_code}}" http://localhost:{porta}

# Iniciar ngrok com --name para identificar o projeto
nohup ngrok http {porta} --name {nome} --log /tmp/{nome}-ngrok.log > /dev/null 2>&1 &
sleep 3

# Pegar URL pública via API do ngrok (filtrando pelo name do projeto)
curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
for t in json.load(sys.stdin)['tunnels']:
    if t.get('name') == '{nome}':
        print(t['public_url']); break
"
```

Depois, envie a URL pública de forma clara e clicável.

**Regras importantes:**
- Sempre rode TODOS os comandos numa única chamada bash — se separar, os processos background morrem
- Sempre use `nohup ... &` para processos de longa duração
- NUNCA mate processos genéricos (ex: `pkill -f ngrok`) — sempre filtre pelo name `{nome}` para não afetar outros projetos
- Sempre use `|| true` após `pkill` para não travar se o processo não existir
- Se o curl retornar 000, espere mais alguns segundos e tente novamente
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
    await msg.reply_text("🔧 Configurando Git...")
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

    # Iniciar dev server + tunnel público
    await msg.reply_text(f"🌐 Iniciando servidor dev (porta {porta}) + tunnel público...")
    try:
        dev_proc = await asyncio.create_subprocess_exec(
            "pnpm", "dev",
            cwd=projeto_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ENV,
        )
        await asyncio.sleep(4)  # esperar o dev server subir

        ngrok_cmd = shutil.which("ngrok") or os.path.expanduser("~/bin/ngrok")
        tunnel_proc = await asyncio.create_subprocess_exec(
            ngrok_cmd, "http", str(porta), "--name", nome,
            cwd=projeto_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ENV,
        )
        await asyncio.sleep(3)  # esperar ngrok subir
        # Pegar URL via API local do ngrok (confiável)
        api_proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "http://localhost:4040/api/tunnels",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        api_out, _ = await asyncio.wait_for(api_proc.communicate(), timeout=10)
        tunnels_data = json.loads(api_out.decode())
        tunnel_url = ""
        for t in tunnels_data.get("tunnels", []):
            if t.get("name") == nome:
                tunnel_url = t.get("public_url", "")
                break

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

    # Perguntar se quer subir pro GitHub
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Sim, subir pro GitHub", callback_data=f"github_novo:sim:{nome}"),
            InlineKeyboardButton("❌ Não", callback_data=f"github_novo:nao:{nome}"),
        ]
    ])
    await msg.reply_text(
        f"Deseja criar o repositório no GitHub e fazer push?",
        reply_markup=teclado,
    )


@autorizado
async def callback_github_novo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para decisão de subir ou não pro GitHub após criar projeto."""
    query = update.callback_query
    await query.answer()
    _, resposta, nome = query.data.split(":", 2)

    if resposta == "nao":
        await query.edit_message_text(
            f"✅ Projeto <b>{nome}</b> criado apenas localmente.\n\n"
            f"📁 <code>{os.path.join(WORKSPACE, nome)}</code>\n"
            f"Stack: Next.js + TS + Tailwind + shadcn/ui + Zod + Biome",
            parse_mode="HTML",
        )
        return

    projeto_dir = os.path.join(WORKSPACE, nome)
    await query.edit_message_text("🔧 Criando repo no GitHub e fazendo push...")

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
        await query.edit_message_text(
            f"Erro ao criar repo no GitHub:\n<pre>{html.escape(gh_erro[:1500])}</pre>\n\n"
            f"Você pode criar manualmente com:\n<code>gh repo create {nome} --public --source {projeto_dir} --push</code>",
            parse_mode="HTML",
        )
    else:
        repo_url = gh_output if gh_output.startswith("http") else f"https://github.com/{gh_output}"
        await query.edit_message_text(
            f"✅ Repo criado no GitHub!\n\n"
            f"🔗 {repo_url}",
            parse_mode="HTML",
        )
