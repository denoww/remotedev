import os
import re
import html
import json
import asyncio
import socket

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from lib.config import PROJETOS, WORKSPACE, descobrir_projetos
from lib.utils import novo_projeto_pendente, ia_apikey_pendente, ia_modelo_pendente, estado, autorizado

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
    """Cria projeto vinext (Vite + Cloudflare) completo com GitHub repo."""
    projeto_dir = os.path.join(WORKSPACE, nome)

    if os.path.exists(projeto_dir):
        await msg.reply_text(f"Já existe um diretório <code>{nome}</code> no workspace.", parse_mode="HTML")
        return

    await msg.reply_text(f"⏳ Criando projeto <b>{nome}</b>...\nIsso pode levar alguns minutos.", parse_mode="HTML")

    # 1) Scaffold base via create-next-app (vinext init precisa dele)
    proc = await asyncio.create_subprocess_exec(
        "pnpm", "create", "next-app@latest", projeto_dir,
        "--typescript", "--tailwind", "--app", "--use-pnpm",
        "--no-eslint", "--no-src-dir", "--no-import-alias",
        "--no-react-compiler", "--no-agents-md", "--yes",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_ENV,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        erro = stderr.decode().strip() or stdout.decode().strip()
        await msg.reply_text(f"Erro ao criar projeto:\n<pre>{html.escape(erro[:2000])}</pre>", parse_mode="HTML")
        return

    if not os.path.exists(projeto_dir):
        await msg.reply_text(
            f"❌ Erro: diretório <code>{nome}</code> não foi criado.\n"
            "Pode ser que o comando tenha pedido input interativo não suportado.",
            parse_mode="HTML",
        )
        return

    # Alocar porta para o dev server (nunca usar 3000)
    porta = _proxima_porta_livre()
    porta_proxy = porta + 50  # proxy para ngrok dockerizado (ex: 5000 → 5050)

    # 2) Converter para vinext (Vite + Cloudflare Workers)
    await msg.reply_text("☁️ Convertendo para vinext...")
    proc = await asyncio.create_subprocess_exec(
        "pnpm", "dlx", "vinext", "init", "--port", str(porta), "--force",
        cwd=projeto_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_ENV,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        erro = stderr.decode().strip() or stdout.decode().strip()
        await msg.reply_text(
            f"⚠️ Aviso ao converter para vinext:\n<pre>{html.escape(erro[:1500])}</pre>",
            parse_mode="HTML",
        )

    # 3) Instalar ferramentas: Zod, Biome, shadcn/ui
    await msg.reply_text("📦 Instalando Zod + Biome + shadcn...")
    extras = [
        ["pnpm", "add", "zod"],
        ["pnpm", "add", "-D", "@biomejs/biome"],
        ["pnpm", "dlx", "shadcn@latest", "init", "-y", "--defaults"],
    ]
    for cmd in extras:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=projeto_dir,
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

    # 4) Fixar scripts vinext no package.json (vinext init cria dev:vinext, queremos dev)
    pkg_path = os.path.join(projeto_dir, "package.json")
    with open(pkg_path) as f:
        pkg = json.load(f)
    pkg["scripts"]["dev"] = f"vinext dev --port {porta}"
    pkg["scripts"]["build"] = "vinext build"
    pkg["scripts"]["start"] = "vinext start"
    pkg["scripts"]["deploy"] = "vinext deploy"
    pkg["scripts"]["deploy:preview"] = "vinext deploy --preview"
    pkg["scripts"]["check"] = "biome check --write ."
    # Remover scripts legado do Next.js/vinext init
    for chave in ("dev:vinext", "lint"):
        pkg["scripts"].pop(chave, None)
    with open(pkg_path, "w") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # 5) Gerar CLAUDE.md
    claude_md = f"""# {nome}

Projeto vinext (Vite + Cloudflare Workers) com TypeScript, Tailwind CSS, shadcn/ui e Zod.

## Stack

- **Framework:** vinext (Vite, deploy no Cloudflare Workers)
- **UI:** Tailwind CSS + shadcn/ui
- **Validação:** Zod
- **Linter/Formatter:** Biome
- **Pacotes:** pnpm

## Comandos

```bash
pnpm dev              # servidor de desenvolvimento (porta {porta})
pnpm build            # build de produção
pnpm check            # linter + formatter (Biome, com auto-fix)
pnpm deploy           # deploy para Cloudflare Workers
pnpm deploy:preview   # deploy para ambiente preview
```

**Porta do dev server: {porta}** — nunca use a porta 3000.

## Convenções

- Código e comentários em português
- Componentes em `components/`
- Componentes de UI (shadcn) em `components/ui/`
- Páginas no App Router em `app/`

## Ligar servidor e URL pública

**SEMPRE que ligar o dev server, inicie o ngrok junto e envie a URL pública.** O usuário acessa remotamente e precisa da URL pública sempre.

### Arquitetura de rede

O ngrok roda dentro de um **container Docker** (serviço do sistema). Ele NÃO consegue acessar `localhost` do host.
Por isso, é necessário um **proxy Node.js** escutando em `0.0.0.0:{porta_proxy}` que redireciona para `127.0.0.1:{porta}`.
O tunnel ngrok é criado via **API REST** (porta 4040) do agente já em execução, apontando para `172.18.0.1:{porta_proxy}` (IP do host visto pelo container).

O ngrok tem plano Hobby com **3 endpoints simultâneos**. Cada projeto usa `subdomain` diferente para não conflitar:
- `seucondominio` → domínio reservado padrão
- `{nome}` → subdomain `{nome}-painel`

**Execute os comandos abaixo EM UMA ÚNICA chamada bash, todos juntos:**

```bash
# Matar processos anteriores SOMENTE deste projeto
pkill -f 'vinext dev.*--port {porta}' 2>/dev/null || true
pkill -f proxy{porta} 2>/dev/null || true
sleep 2

# Iniciar dev server em background
nohup pnpm dev > /tmp/{nome}-dev.log 2>&1 &
sleep 5

# Verificar se o servidor subiu
curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:{porta}

# Iniciar proxy Node.js (0.0.0.0:{porta_proxy} -> 127.0.0.1:{porta}) para o ngrok dockerizado alcançar
cat > /tmp/proxy{porta}.js << 'PROXYEOF'
const http = require("http");
const net = require("net");
const proxy = http.createServer((req, res) => {{
  const opts = {{ hostname: "127.0.0.1", port: {porta}, path: req.url, method: req.method, headers: {{ ...req.headers, host: "localhost:{porta}" }} }};
  const p = http.request(opts, (pr) => {{ res.writeHead(pr.statusCode, pr.headers); pr.pipe(res); }});
  p.on("error", (e) => {{ res.writeHead(502); res.end("proxy error: " + e.message); }});
  req.pipe(p);
}});
proxy.on("upgrade", (req, socket, head) => {{
  const conn = net.connect({porta}, "127.0.0.1", () => {{
    const headers = {{ ...req.headers, host: "localhost:{porta}" }};
    let reqLine = req.method + " " + req.url + " HTTP/1.1\\r\\n";
    for (const [k, v] of Object.entries(headers)) reqLine += k + ": " + v + "\\r\\n";
    reqLine += "\\r\\n";
    conn.write(reqLine); conn.write(head); socket.pipe(conn).pipe(socket);
  }});
  conn.on("error", () => socket.destroy());
}});
proxy.listen({porta_proxy}, "0.0.0.0", () => console.log("proxy on 0.0.0.0:{porta_proxy} -> 127.0.0.1:{porta}"));
PROXYEOF
nohup node /tmp/proxy{porta}.js > /tmp/proxy{porta}.log 2>&1 &
sleep 2

# Remover tunnel {nome} anterior (se existir) e recriar via API do ngrok
curl -s -X DELETE http://localhost:4040/api/tunnels/{nome} 2>/dev/null || true
sleep 1

# Criar tunnel com subdomain próprio (não conflita com outros projetos)
curl -s -X POST http://localhost:4040/api/tunnels \\
  -H "Content-Type: application/json" \\
  -d '{{"addr": "http://172.18.0.1:{porta_proxy}", "proto": "http", "name": "{nome}", "subdomain": "{nome}-painel"}}' | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('public_url', d))
"
```

Depois, envie a URL pública de forma clara e clicável.

**Regras importantes:**
- Sempre rode TODOS os comandos numa única chamada bash — se separar, os processos background morrem
- Sempre use `nohup ... &` para processos de longa duração
- NUNCA mate processos genéricos (ex: `pkill -f ngrok`) — sempre filtre pelo name `{nome}` para não afetar outros projetos
- Sempre use `|| true` após `pkill` para não travar se o processo não existir
- O tunnel é criado via API REST na porta 4040 (agente ngrok do sistema), NÃO via CLI `ngrok http`
- Usar `subdomain: "{nome}-painel"` para ter URL separada de outros projetos
- O proxy é necessário porque o ngrok roda em container Docker e não alcança localhost do host
"""
    with open(os.path.join(projeto_dir, "CLAUDE.md"), "w") as f:
        f.write(claude_md)

    # 6) Configurar Biome
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
        ["git", "commit", "-m", "feat: init projeto com vinext + TS + Tailwind + shadcn + Zod + Biome"],
    ]
    for cmd in git_cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=projeto_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
        await proc.communicate()

    # Iniciar dev server + proxy + tunnel público via API ngrok
    await msg.reply_text(f"🌐 Iniciando servidor dev (porta {porta}) + proxy ({porta_proxy}) + tunnel público...")
    try:
        dev_proc = await asyncio.create_subprocess_exec(
            "pnpm", "dev",
            cwd=projeto_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ENV,
        )
        await asyncio.sleep(5)  # esperar o dev server subir

        # Proxy Node.js (0.0.0.0:porta_proxy -> 127.0.0.1:porta) para o ngrok dockerizado alcançar
        proxy_script = f"""
const http = require("http");
const net = require("net");
const proxy = http.createServer((req, res) => {{
  const opts = {{ hostname: "127.0.0.1", port: {porta}, path: req.url, method: req.method, headers: {{ ...req.headers, host: "localhost:{porta}" }} }};
  const p = http.request(opts, (pr) => {{ res.writeHead(pr.statusCode, pr.headers); pr.pipe(res); }});
  p.on("error", (e) => {{ res.writeHead(502); res.end("proxy error: " + e.message); }});
  req.pipe(p);
}});
proxy.on("upgrade", (req, socket, head) => {{
  const conn = net.connect({porta}, "127.0.0.1", () => {{
    const headers = {{ ...req.headers, host: "localhost:{porta}" }};
    let reqLine = req.method + " " + req.url + " HTTP/1.1\\r\\n";
    for (const [k, v] of Object.entries(headers)) reqLine += k + ": " + v + "\\r\\n";
    reqLine += "\\r\\n";
    conn.write(reqLine); conn.write(head); socket.pipe(conn).pipe(socket);
  }});
  conn.on("error", () => socket.destroy());
}});
proxy.listen({porta_proxy}, "0.0.0.0", () => console.log("proxy on 0.0.0.0:{porta_proxy} -> 127.0.0.1:{porta}"));
"""
        proxy_path = f"/tmp/proxy{porta}.js"
        with open(proxy_path, "w") as f:
            f.write(proxy_script)

        proxy_proc = await asyncio.create_subprocess_exec(
            "node", proxy_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=_ENV,
        )
        await asyncio.sleep(2)

        # Remover tunnel anterior (se existir) via API do ngrok
        try:
            del_proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "DELETE", f"http://localhost:4040/api/tunnels/{nome}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(del_proc.communicate(), timeout=5)
        except Exception:
            pass
        await asyncio.sleep(1)

        # Criar tunnel via API REST do ngrok (agente dockerizado na porta 4040)
        tunnel_payload = json.dumps({
            "addr": f"http://172.18.0.1:{porta_proxy}",
            "proto": "http",
            "name": nome,
            "subdomain": f"{nome}-painel",
        })
        tunnel_url = ""
        for tentativa in range(3):
            try:
                api_proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-X", "POST", "http://localhost:4040/api/tunnels",
                    "-H", "Content-Type: application/json",
                    "-d", tunnel_payload,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                api_out, _ = await asyncio.wait_for(api_proc.communicate(), timeout=10)
                data = json.loads(api_out.decode())
                tunnel_url = data.get("public_url", "")
                if tunnel_url:
                    break
            except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
                await asyncio.sleep(2)
                continue

        _tunnel_procs[nome] = {"dev": dev_proc, "proxy": proxy_proc, "porta": porta, "porta_proxy": porta_proxy}

        if tunnel_url:
            await msg.reply_text(
                f"🌐 <b>URL pública:</b> {tunnel_url}\n"
                f"🏠 <b>URL local:</b> http://localhost:{porta}\n\n"
                f"Tunnel ativo enquanto o bot estiver rodando.",
                parse_mode="HTML",
            )
        else:
            await msg.reply_text(
                f"⚠️ Tunnel ngrok criado mas não consegui obter a URL pública.\n\n"
                f"🏠 <b>URL local:</b> http://localhost:{porta}\n"
                f"🔧 Tente: <code>/bash curl -s http://localhost:4040/api/tunnels</code> para verificar manualmente.",
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

    # Perguntar se quer análise com IA
    await _perguntar_ia(msg, nome)


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
            f"Stack: vinext + TS + Tailwind + shadcn/ui + Zod + Biome",
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


# ══════════════════════════════════════════════════════════════════════
# ANÁLISE COM IA — Configuração de API key + CLAUDE.md
# ══════════════════════════════════════════════════════════════════════

_IA_PROVIDERS = {
    "gemini": {
        "nome": "Gemini",
        "env_var": "GEMINI_API_KEY",
        "pacote": "npm:@google/genai",
        "como_obter": (
            "1. Acesse: https://aistudio.google.com/apikey\n"
            "2. Clique em <b>Create API Key</b>\n"
            "3. Selecione ou crie um projeto Google Cloud\n"
            "4. Copie a key gerada"
        ),
    },
    "openai": {
        "nome": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "pacote": "npm:openai",
        "como_obter": (
            "1. Acesse: https://platform.openai.com/api-keys\n"
            "2. Clique em <b>Create new secret key</b>\n"
            "3. Dê um nome e copie a key gerada\n"
            "⚠️ Requer créditos adicionados em https://platform.openai.com/settings/organization/billing"
        ),
    },
    "anthropic": {
        "nome": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "pacote": "npm:@anthropic-ai/sdk",
        "como_obter": (
            "1. Acesse: https://console.anthropic.com/settings/keys\n"
            "2. Clique em <b>Create Key</b>\n"
            "3. Dê um nome e copie a key gerada\n"
            "⚠️ Requer créditos adicionados em https://console.anthropic.com/settings/plans"
        ),
    },
}


async def _perguntar_ia(msg, nome: str):
    """Pergunta se o usuário quer adicionar análise com IA ao projeto."""
    teclado = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Sim", callback_data=f"ia_analise:sim:{nome}"),
            InlineKeyboardButton("❌ Não", callback_data=f"ia_analise:nao:{nome}"),
        ]
    ])
    await msg.reply_text(
        "🤖 Pretende usar <b>análise de dados com IA</b> neste projeto?",
        parse_mode="HTML",
        reply_markup=teclado,
    )


@autorizado
async def callback_ia_analise(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para decisão de usar ou não análise com IA."""
    query = update.callback_query
    await query.answer()
    _, resposta, nome = query.data.split(":", 2)

    if resposta == "nao":
        await query.edit_message_text("👍 Projeto criado sem análise com IA.")
        return

    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("💻 Claude Code (computador)", callback_data=f"ia_provider:claudecode:{nome}")],
        [InlineKeyboardButton("🔷 Gemini", callback_data=f"ia_provider:gemini:{nome}")],
        [InlineKeyboardButton("🟢 OpenAI", callback_data=f"ia_provider:openai:{nome}")],
        [InlineKeyboardButton("🟠 Anthropic (Claude API)", callback_data=f"ia_provider:anthropic:{nome}")],
    ])
    await query.edit_message_text(
        "Qual provider de IA você quer usar?\n\n"
        "💻 <b>Claude Code</b> — usa o CLI local do computador (plano Max, sem custo extra de API)\n"
        "🔷🟢🟠 — usam API key do provider (créditos à parte)",
        parse_mode="HTML",
        reply_markup=teclado,
    )


@autorizado
async def callback_ia_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para escolha do provider de IA."""
    query = update.callback_query
    await query.answer()
    _, provider, nome = query.data.split(":", 2)

    # Claude Code CLI — não precisa de API key, vai direto
    if provider == "claudecode":
        chat_id = update.effective_chat.id
        await query.edit_message_text("💻 Configurando Claude Code CLI...")
        await _finalizar_config_claudecode(chat_id, nome, msg=query.message)
        return

    info = _IA_PROVIDERS[provider]
    chat_id = update.effective_chat.id
    ia_apikey_pendente[chat_id] = {"nome": nome, "provider": provider}

    await query.edit_message_text(
        f"🔑 Envie a <b>API key</b> do {info['nome']}:\n\n"
        f"<i>(variável: <code>{info['env_var']}</code>)</i>\n\n"
        f"<b>Como obter:</b>\n{info['como_obter']}\n\n"
        "Ou /cancelar para pular.",
        parse_mode="HTML",
    )


async def _listar_modelos(provider: str, apikey: str) -> list[str]:
    """Lista modelos disponíveis via API do provider. Retorna lista de IDs."""
    modelos = []
    try:
        if provider == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={apikey}"
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-f", url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                data = json.loads(stdout.decode())
                for m in data.get("models", []):
                    nome_modelo = m.get("name", "").removeprefix("models/")
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        modelos.append(nome_modelo)

        elif provider == "openai":
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-f", "https://api.openai.com/v1/models",
                "-H", f"Authorization: Bearer {apikey}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                data = json.loads(stdout.decode())
                for m in data.get("data", []):
                    mid = m.get("id", "")
                    if mid.startswith(("gpt-", "o1", "o3", "o4")):
                        modelos.append(mid)

        elif provider == "anthropic":
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-f", "https://api.anthropic.com/v1/models",
                "-H", f"x-api-key: {apikey}", "-H", "anthropic-version: 2023-06-01",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                data = json.loads(stdout.decode())
                for m in data.get("data", []):
                    modelos.append(m.get("id", ""))
    except Exception:
        pass

    modelos.sort()
    return modelos


async def processar_apikey_ia(chat_id: int, apikey: str, msg):
    """Recebe a API key, valida listando modelos e oferece escolha."""
    dados = ia_apikey_pendente.pop(chat_id)
    nome = dados["nome"]
    provider = dados["provider"]
    info = _IA_PROVIDERS[provider]

    await msg.reply_text(f"🔍 Validando API key e listando modelos do {info['nome']}...")

    modelos = await _listar_modelos(provider, apikey)

    if not modelos:
        # Key inválida ou API indisponível — prosseguir sem escolha de modelo
        await msg.reply_text(
            "⚠️ Não foi possível listar modelos (key inválida ou API indisponível).\n"
            "Prosseguindo sem modelo específico — você pode configurar depois.",
        )
        await _finalizar_config_ia(chat_id, nome, provider, apikey, modelo=None, msg=msg)
        return

    # Limitar a 20 modelos para não estourar o teclado do Telegram
    modelos_exibir = modelos[:20]

    # Salvar estado pendente para escolha de modelo
    ia_modelo_pendente[chat_id] = {
        "nome": nome,
        "provider": provider,
        "apikey": apikey,
        "modelos": modelos_exibir,
    }

    # Montar teclado com modelos (1 por linha)
    botoes = [[InlineKeyboardButton(m, callback_data=f"ia_modelo:{i}")] for i, m in enumerate(modelos_exibir)]
    botoes.append([InlineKeyboardButton("⏭️ Pular (sem modelo específico)", callback_data="ia_modelo:pular")])
    teclado = InlineKeyboardMarkup(botoes)

    await msg.reply_text(
        f"✅ API key válida! {len(modelos)} modelo(s) encontrado(s).\n\n"
        "Escolha o modelo que deseja usar:",
        reply_markup=teclado,
    )


@autorizado
async def callback_ia_modelo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para escolha do modelo de IA."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if chat_id not in ia_modelo_pendente:
        await query.edit_message_text("⚠️ Sessão expirada. Tente criar o projeto novamente.")
        return

    dados = ia_modelo_pendente.pop(chat_id)
    escolha = query.data.split(":", 1)[1]

    if escolha == "pular":
        modelo = None
        await query.edit_message_text("👍 Prosseguindo sem modelo específico.")
    else:
        idx = int(escolha)
        modelo = dados["modelos"][idx]
        await query.edit_message_text(f"🤖 Modelo selecionado: <b>{modelo}</b>", parse_mode="HTML")

    await _finalizar_config_ia(
        chat_id, dados["nome"], dados["provider"], dados["apikey"],
        modelo=modelo, msg=query.message,
    )


async def _finalizar_config_ia(chat_id: int, nome: str, provider: str, apikey: str, modelo: str | None, msg):
    """Salva API key + modelo no .env, instala SDK e atualiza CLAUDE.md."""
    info = _IA_PROVIDERS[provider]
    projeto_dir = os.path.join(WORKSPACE, nome)

    # 1) Salvar no .env do projeto
    env_path = os.path.join(projeto_dir, ".env")
    linhas_existentes = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            linhas_existentes = f.readlines()

    # Remover linhas antigas das mesmas vars, se existirem
    vars_remover = {info['env_var'], f"{info['env_var'].replace('_API_KEY', '')}_MODEL"}
    linhas_existentes = [l for l in linhas_existentes if not any(l.startswith(f"{v}=") for v in vars_remover)]
    linhas_existentes.append(f"{info['env_var']}={apikey}\n")
    if modelo:
        var_modelo = info['env_var'].replace('_API_KEY', '') + '_MODEL'
        linhas_existentes.append(f"{var_modelo}={modelo}\n")

    with open(env_path, "w") as f:
        f.writelines(linhas_existentes)

    # Garantir que .env está no .gitignore
    gitignore_path = os.path.join(projeto_dir, ".gitignore")
    if os.path.exists(gitignore_path):
        with open(gitignore_path) as f:
            conteudo = f.read()
        if ".env" not in conteudo.split("\n"):
            with open(gitignore_path, "a") as f:
                f.write("\n.env\n")

    # 2) Instalar SDK do provider
    await msg.reply_text(f"📦 Instalando SDK do {info['nome']}...")
    proc = await asyncio.create_subprocess_exec(
        "pnpm", "add", info["pacote"],
        cwd=projeto_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_ENV,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        erro = stderr.decode().strip() or stdout.decode().strip()
        await msg.reply_text(
            f"⚠️ Erro ao instalar SDK:\n<pre>{html.escape(erro[:1500])}</pre>",
            parse_mode="HTML",
        )

    # 3) Atualizar CLAUDE.md com seção de análise de dados
    modelo_texto = f"`{modelo}`" if modelo else "padrão do provider"
    var_modelo_texto = ""
    if modelo:
        var_modelo = info['env_var'].replace('_API_KEY', '') + '_MODEL'
        var_modelo_texto = f"\n- **Modelo:** `{modelo}` (variável: `{var_modelo}`)"

    claude_md_path = os.path.join(projeto_dir, "CLAUDE.md")
    secao_ia = f"""

## Análise de Dados com IA

Este projeto usa a API do **{info['nome']}** para análise de dados.

- **Provider:** {info['nome']}
- **SDK:** `{info['pacote']}`
- **Variável de ambiente:** `{info['env_var']}` (configurada no `.env`){var_modelo_texto}

### Como usar

Quando o usuário pedir para analisar dados, usar a API do {info['nome']} para processar.
A API key está disponível via `process.env.{info['env_var']}`.
{f"O modelo a usar está em `process.env.{info['env_var'].replace('_API_KEY', '')}_MODEL`." if modelo else ""}

### Feedback em tempo real para o usuário

**IMPORTANTE:** Chamadas de IA com busca web ou processamento longo DEVEM dar feedback visual ao usuário para que ele não pense que a aplicação travou.

- Use **streaming** (Server-Sent Events ou NDJSON) para enviar o texto conforme a IA gera
- Envie **eventos de status** separados do texto, indicando o que a IA está fazendo:
  - "Conectando com a IA..."
  - "Buscando na web (1/N): ..."
  - "Processando resultados..."
  - "Escrevendo análise..."
- No frontend, exiba esses status em um **indicador visual** (barra ou badge) acima do resultado
- NUNCA deixe a tela parada sem feedback por mais de 2-3 segundos durante uma chamada de IA

### Exemplos de análise que podem ser solicitadas

- Análise de tendências e padrões nos dados
- Resumos e insights a partir de datasets
- Classificação e categorização de informações
- Geração de relatórios baseados em dados
"""
    if os.path.exists(claude_md_path):
        with open(claude_md_path, "a") as f:
            f.write(secao_ia)

    modelo_linha = f"\n🤖 <b>Modelo:</b> <code>{modelo}</code>" if modelo else ""
    await msg.reply_text(
        f"✅ IA configurada!\n\n"
        f"🤖 <b>Provider:</b> {info['nome']}{modelo_linha}\n"
        f"🔑 <b>Env:</b> <code>{info['env_var']}</code>\n"
        f"📦 <b>SDK:</b> <code>{info['pacote']}</code>\n\n"
        f"A API key foi salva no <code>.env</code> e a estrutura de análise foi adicionada ao <code>CLAUDE.md</code>.",
        parse_mode="HTML",
    )


async def _finalizar_config_claudecode(chat_id: int, nome: str, msg):
    """Configura o projeto para usar Claude Code CLI (computador local) em vez de API."""
    projeto_dir = os.path.join(WORKSPACE, nome)
    claude_md_path = os.path.join(projeto_dir, "CLAUDE.md")

    secao_ia = f"""

## Análise de Dados com IA

Este projeto usa o **Claude Code CLI** (computador local) para análise de dados.
Isso utiliza o plano Max do usuário, sem custo extra de API.

- **Provider:** Claude Code CLI (local)
- **Comando:** `claude -p`
- **Sem necessidade de API key** — usa a autenticação do Claude Code instalado na máquina

### Como usar

Para chamadas de IA nas API routes, usar o Claude Code CLI via `spawn`:

```typescript
import {{ spawn }} from "node:child_process";

const claudePath = `${{process.env.HOME}}/.local/bin/claude`;
const proc = spawn(claudePath, [
  "-p", prompt,
  "--output-format", "stream-json",
  "--verbose",
  "--allowedTools", "WebSearch,WebFetch",
], {{
  env: {{ ...process.env, PATH: `${{process.env.HOME}}/.local/bin:${{process.env.PATH}}` }},
  stdio: ["pipe", "pipe", "pipe"],
}});
```

O stream JSON retorna eventos:
- `{{"type":"assistant","message":{{"content":[{{"type":"text","text":"..."}}]}}}}` → texto
- `{{"type":"assistant","message":{{"content":[{{"type":"tool_use","name":"WebSearch"}}]}}}}` → busca web
- `{{"type":"result","result":"..."}}` → resultado final

### Feedback em tempo real para o usuário

**IMPORTANTE:** Chamadas de IA com busca web ou processamento longo DEVEM dar feedback visual ao usuário para que ele não pense que a aplicação travou.

- Use **streaming** (NDJSON) para enviar o texto conforme a IA gera
- Envie **eventos de status** separados do texto, indicando o que a IA está fazendo:
  - "Conectando com a IA..."
  - "Buscando na web (1/N): ..."
  - "Processando resultados..."
  - "Escrevendo análise..."
- No frontend, exiba esses status em um **indicador visual** (barra ou badge) acima do resultado
- NUNCA deixe a tela parada sem feedback por mais de 2-3 segundos durante uma chamada de IA

### Exemplos de análise que podem ser solicitadas

- Análise de tendências e padrões nos dados
- Resumos e insights a partir de datasets
- Classificação e categorização de informações
- Geração de relatórios baseados em dados
"""
    if os.path.exists(claude_md_path):
        with open(claude_md_path, "a") as f:
            f.write(secao_ia)

    await msg.reply_text(
        f"✅ IA configurada!\n\n"
        f"💻 <b>Provider:</b> Claude Code CLI (computador local)\n"
        f"🔑 <b>API key:</b> não necessária\n"
        f"📦 <b>Comando:</b> <code>claude -p</code>\n\n"
        f"Usa o plano Max do Claude Code — sem custo extra de API.\n"
        f"Instruções adicionadas ao <code>CLAUDE.md</code>.",
        parse_mode="HTML",
    )
