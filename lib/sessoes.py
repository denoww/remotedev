"""
/sessoes_listar — sessões Claude Code vivas nesta máquina (as de terminal, não as
do bot), pra ver de longe em que ponto cada uma está e mandar uma mensagem sem
tomar o controle delas.

Duas fontes, nenhuma inventada por aqui:
  `claude agents --json`   lista as sessões vivas (pid, cwd, nome, status) — o
                            mesmo dado que a ferramenta ListAgents usa por trás.
  o SendMessage delas       envia texto pra uma sessão viva sem "roubar": ela
                            processa como um turno normal (mesmo se estiver
                            ocupada, fica na fila) e não temos como forçar entrada
                            direto no `.jsonl` — testado ao vivo antes de escrever
                            este arquivo: sessão idle recebeu, processou como
                            turno de verdade (gastou tokens, respondeu, voltou pra
                            idle) e o histórico não bifurcou.

A volta (o que a sessão responde) NÃO vem pelo mesmo canal: quem entrega a
mensagem é um `claude -p` descartável que morre assim que confirma a entrega, e
a sessão-alvo, ao tentar responder pra ele, esbarra num socket morto ("a sessão
remotedev-xx sumiu antes de eu enviar"). Então o eco é OBSERVACIONAL: depois de
entregar, o bot acompanha o `.jsonl` da sessão-alvo e devolve pro Telegram o que
ela produziu naquele turno — a mensagem que ela endereçou ao remetente (mesmo
que a entrega tenha falhado) ou, na falta dela, o que ela falou. Não depende de
cooperação da sessão-alvo nem de ninguém ficar vivo esperando.
"""
import os
import re
import glob
import json
import time
import asyncio
import subprocess

from lib.config import WORKSPACE, BOT_REPO_DIR

AGENTS_TIMEOUT = 10
ENVIO_TIMEOUT = 90
ECO_TIMEOUT = 900      # até 15 min esperando a sessão responder (ela pode estar ocupada)
ECO_POLL = 3           # de quanto em quanto tempo reler o transcript
ECO_SILENCIO = 12      # silêncio no transcript que sugere fim de turno (confirmado pelo status)
ECO_MAX_CHARS = 8000   # teto do que devolvemos pro Telegram
PROJETOS_CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
CAUDA_BYTES = 200_000  # quanto ler do fim do transcript pra resumir "o que" a sessão está fazendo

# chat_id → lista de sessões da última /sessoes_listar (resolve os botões)
sessoes_cache = {}
# chat_id → {"nome": ...} sessão escolhida aguardando a próxima mensagem (texto/foto) pra repassar
resposta_pendente = {}

_ROTULO_STATUS = {"idle": "💤 parada", "busy": "⚙️ trabalhando", "shell": "🐚 no shell",
                   "waiting": "🖐️ esperando você"}


def _do_workspace(cwd):
    if not cwd:
        return False
    try:
        cwd_r = os.path.realpath(cwd)
        ws_r = os.path.realpath(WORKSPACE)
    except OSError:
        return False
    return cwd_r == ws_r or cwd_r.startswith(ws_r + os.sep)


def listar_peers(so_workspace=True):
    """Sessões Claude vivas nesta máquina — direto do `claude agents --json`."""
    try:
        res = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=AGENTS_TIMEOUT,
        )
        sessoes = json.loads(res.stdout or "[]")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []
    if not isinstance(sessoes, list):
        return []
    if so_workspace:
        sessoes = [s for s in sessoes if _do_workspace(s.get("cwd"))]
    return sessoes


def _uma_linha(texto, limite=300):
    return " ".join((texto or "").split())[:limite]


# O detalhe da sessão mostra a última fala inteira. Antes tudo passava por
# _uma_linha e cortava em 300 chars, e o texto chegava pela metade ("O teste com
# teclado real fica para o de"). O que passa do limite de uma mensagem do
# Telegram o bot manda em mensagens seguidas; este teto só segura falas absurdas.
FALA_MAX_CHARS = 20000


def _texto_preservado(texto, limite=FALA_MAX_CHARS):
    """Mantém parágrafos e quebras; só apara espaço sobrando e limita o tamanho."""
    linhas = [l.rstrip() for l in (texto or "").strip().splitlines()]
    limpo = re.sub(r"\n{3,}", "\n\n", "\n".join(linhas))
    if len(limpo) > limite:
        limpo = limpo[:limite].rstrip() + "…"
    return limpo


def _transcript(session_id):
    achados = glob.glob(os.path.join(PROJETOS_CLAUDE_DIR, "*", f"{session_id}.jsonl"))
    return achados[0] if achados else None


def _ler_cauda_json(path, tam_max=CAUDA_BYTES):
    try:
        tam = os.path.getsize(path)
        with open(path, "rb") as f:
            if tam > tam_max:
                f.seek(tam - tam_max)
            bruto = f.read()
    except OSError:
        return []
    linhas = bruto.split(b"\n")
    if tam > tam_max:
        linhas = linhas[1:]  # a primeira veio cortada no meio
    saida = []
    for linha in linhas:
        if linha.strip():
            try:
                saida.append(json.loads(linha))
            except ValueError:
                pass
    return saida


def _ultima_atividade(session_id):
    """Epoch da última gravação no transcript da sessão (0 se não achar)."""
    path = _transcript(session_id)
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def _resumo_transcript(session_id):
    """Do fim do transcript: último pedido humano, última fala do Claude, tool pendente."""
    path = _transcript(session_id)
    if not path:
        return {}
    ultimo_pedido = ultima_fala = tool_pendente = None
    for entrada in _ler_cauda_json(path):
        if entrada.get("isSidechain"):  # subagente — não é a conversa principal
            continue
        msg = entrada.get("message") or {}
        cont = msg.get("content")
        if isinstance(cont, list):
            blocos = cont
        elif isinstance(cont, str):
            blocos = [{"type": "text", "text": cont}]
        else:
            blocos = []
        tipo = entrada.get("type")
        if tipo == "user":
            humano = entrada.get("userType") == "external" and not entrada.get("isMeta")
            for b in blocos:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    tool_pendente = None
                elif b.get("type") == "text" and humano:
                    t = (b.get("text") or "").strip()
                    if t and not t.startswith("<"):
                        ultimo_pedido, ultima_fala = _texto_preservado(t), None
        elif tipo == "assistant":
            for b in blocos:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    ultima_fala = _texto_preservado(b["text"])
                elif b.get("type") == "tool_use":
                    tool_pendente = b.get("name")
    return {"ultimo_pedido": ultimo_pedido, "ultima_fala": ultima_fala, "tool_pendente": tool_pendente}


def coletar(so_workspace=True):
    """Sessões vivas + resumo do que cada uma está fazendo, prontas pra exibir."""
    saida = []
    for s in listar_peers(so_workspace=so_workspace):
        saida.append({**s, **_resumo_transcript(s.get("sessionId", "")),
                      "ultima_atividade": _ultima_atividade(s.get("sessionId", ""))})
    # Quem está trabalhando agora vem primeiro; depois as paradas, das que
    # trabalharam há pouco pras que dormem há dias (o transcript só é escrito
    # quando a sessão faz algo, então o mtime dele é o "trabalhou por último").
    # No shell (sessão largada num prompt) vai pro fim. Dentro de cada grupo,
    # a atividade mais recente sobe.
    grupo = {"busy": 0, "idle": 1, "shell": 2}
    saida.sort(key=lambda s: (grupo.get(s.get("status"), 1), -s["ultima_atividade"]))
    return saida


def rotulo_status(status):
    return _ROTULO_STATUS.get(status, status or "?")


def o_que(sessao):
    if sessao.get("tool_pendente") and sessao.get("status") == "busy":
        return f"🔧 {sessao['tool_pendente']}"
    return sessao.get("ultima_fala") or sessao.get("ultimo_pedido") or "sessão nova, sem conversa"


def _dur(segundos):
    segundos = int(max(0, segundos))
    if segundos < 60:
        return f"{segundos}s"
    if segundos < 3600:
        return f"{segundos // 60}min"
    if segundos < 86400:
        return f"{segundos // 3600}h{(segundos % 3600) // 60:02d}"
    return f"{segundos // 86400}d"


def ha_atividade(sessao):
    """Há quanto tempo a sessão mexeu no transcript pela última vez."""
    ultima = sessao.get("ultima_atividade")
    if not ultima:
        return "?"
    return _dur(time.time() - ultima)


def ha_quanto(sessao):
    inicio = sessao.get("startedAt")
    if not inicio:
        return "?"
    return _dur(time.time() - inicio / 1000.0)


NOTA_CANAL = (
    "\n\n---\n(Mensagem vinda do Telegram do Rodrigo. Responda normalmente nesta "
    "sessão: o bot lê a sua resposta no transcript e devolve pra ele. Não precisa "
    "SendMessage de volta — quem entregou isto foi um processo efêmero, que já morreu.)"
)


def enviar_mensagem_peer(nome, texto, nota=True):
    """Dispara um `claude -p` descartável que só chama SendMessage(to=nome, message=texto).

    Não é resume da sessão alvo nem toca no `.jsonl` dela diretamente — é outra
    sessão (esta, efêmera) conversando com ela pelo canal de mensagens do próprio
    Claude Code. Por isso não rouba: a sessão alvo recebe como se fosse uma
    mensagem de outra pessoa, processa quando puder, e o dono continua livre pra
    usar o terminal dela a qualquer momento.
    """
    if nota:
        texto = texto + NOTA_CANAL
    instrucao = (
        f"Chame a ferramenta SendMessage uma única vez com to={json.dumps(nome)} e "
        f"message={json.dumps(texto)}. Não faça mais nada além disso — não leia "
        f"arquivos, não investigue nada, apenas chame a ferramenta e pare."
    )
    try:
        res = subprocess.run(
            ["claude", "-p", "-", "--dangerously-skip-permissions",
             "--model", "haiku", "--output-format", "json", "--tools", "SendMessage"],
            input=instrucao, capture_output=True, text=True,
            timeout=ENVIO_TIMEOUT, cwd=BOT_REPO_DIR,
        )
        dados = json.loads(res.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return False, str(e)
    if dados.get("is_error"):
        return False, dados.get("result") or res.stderr.strip() or "erro desconhecido"
    return True, dados.get("result") or "enviado"


# ─────────────────────────────────────────────────────────────────────
# Eco da resposta: acompanha o transcript da sessão-alvo depois do envio
# ─────────────────────────────────────────────────────────────────────

_RE_PEER = re.compile(r'<cross-session-message from="([^"]*)" from-name="([^"]*)"')


def offset_transcript(session_id):
    """Onde o transcript da sessão está AGORA — marco pra só olhar o que vier depois."""
    path = _transcript(session_id)
    if not path:
        return None, 0
    try:
        return path, os.path.getsize(path)
    except OSError:
        return path, 0


def _ler_desde(path, offset):
    """Entradas completas gravadas depois de `offset`. Linha pela metade fica pra próxima."""
    try:
        tam = os.path.getsize(path)
    except OSError:
        return [], offset
    if tam < offset:  # transcript recomeçou
        offset = 0
    if tam == offset:
        return [], offset
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            bruto = f.read(tam - offset)
    except OSError:
        return [], offset
    corte = bruto.rfind(b"\n")
    if corte == -1:
        return [], offset  # só tem linha incompleta; espera terminar de escrever
    consumido = bruto[:corte + 1]
    entradas = []
    for linha in consumido.split(b"\n"):
        if linha.strip():
            try:
                entradas.append(json.loads(linha))
            except ValueError:
                pass
    return entradas, offset + len(consumido)


def _texto_bruto(conteudo):
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return json.dumps(conteudo, ensure_ascii=False)
    return ""


def _e_nossa_entrega(bruto, texto_enviado, from_name):
    """A mensagem que acabou de chegar na sessão é a que ACABAMOS de mandar?"""
    assinatura = " ".join((texto_enviado or "").split())[:80]
    if assinatura and assinatura in " ".join(bruto.split()):
        return True
    # A entrega sai de um `claude -p` rodando no diretório do bot, então o nome
    # dele começa com "remotedev-". Rede de segurança pra texto reformatado.
    return from_name.startswith(os.path.basename(BOT_REPO_DIR) + "-")


def _ainda_ocupada(nome):
    """Sessão segue trabalhando? Se sumiu da lista (morreu), não está."""
    for s in listar_peers(so_workspace=False):
        if s.get("name") == nome:
            return s.get("status") == "busy"
    return False


async def aguardar_resposta(session_id, nome, texto_enviado, path, offset,
                            timeout=ECO_TIMEOUT, silencio=ECO_SILENCIO):
    """
    Espera a sessão-alvo processar a mensagem e devolve o texto da resposta dela.

    Prefere o que ela endereçou ao remetente (o SendMessage de volta, que morre no
    socket efêmero mas cujo conteúdo está no transcript); na falta, o que ela falou
    no turno. `None` se nada vier dentro do timeout.
    """
    if not path:
        return None

    limite = time.time() + timeout
    ultima_novidade = time.time()
    chegou = False
    remetente = set()
    falas, enderecadas = [], []

    while time.time() < limite:
        await asyncio.sleep(ECO_POLL)
        entradas, offset = await asyncio.to_thread(_ler_desde, path, offset)
        if entradas:
            ultima_novidade = time.time()

        for e in entradas:
            if e.get("isSidechain"):  # subagente não é a conversa principal
                continue
            msg = e.get("message") or {}
            bruto = _texto_bruto(msg.get("content"))
            tipo = e.get("type")

            if tipo == "user":
                m = _RE_PEER.search(bruto)
                if m and _e_nossa_entrega(bruto, texto_enviado, m.group(2)):
                    # nossa mensagem entrou na sessão: o turno dela começa aqui
                    chegou = True
                    remetente = {m.group(1), m.group(2)}
                    falas, enderecadas = [], []
                continue

            if not chegou or tipo != "assistant":
                continue
            for b in (msg.get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    falas.append(b["text"].strip())
                elif b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                    entrada = b.get("input") or {}
                    if entrada.get("to") in remetente and (entrada.get("message") or "").strip():
                        enderecadas.append(entrada["message"].strip())

        if chegou and (falas or enderecadas) and time.time() - ultima_novidade >= silencio:
            if await asyncio.to_thread(_ainda_ocupada, nome):
                continue  # calou porque está numa tool longa, não porque terminou
            break

    resposta = "\n\n".join(enderecadas or falas).strip()
    return resposta[:ECO_MAX_CHARS] or None
