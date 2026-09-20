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

O que NÃO dá: não tem eco de volta pro Telegram. Quem manda a mensagem (um
`claude -p` descartável, sem relação com a sessão alvo) morre assim que confirma
a entrega — a resposta da sessão-alvo fica só no terminal dela.
"""
import os
import glob
import json
import time
import subprocess

from lib.config import WORKSPACE, BOT_REPO_DIR

AGENTS_TIMEOUT = 10
ENVIO_TIMEOUT = 90
PROJETOS_CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
CAUDA_BYTES = 200_000  # quanto ler do fim do transcript pra resumir "o que" a sessão está fazendo

# chat_id → lista de sessões da última /sessoes_listar (resolve os botões)
sessoes_cache = {}
# chat_id → {"nome": ...} sessão escolhida aguardando a próxima mensagem (texto/foto) pra repassar
resposta_pendente = {}

_ROTULO_STATUS = {"idle": "💤 parada", "busy": "⚙️ trabalhando", "shell": "🐚 no shell"}


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
                        ultimo_pedido, ultima_fala = _uma_linha(t), None
        elif tipo == "assistant":
            for b in blocos:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    ultima_fala = _uma_linha(b["text"])
                elif b.get("type") == "tool_use":
                    tool_pendente = b.get("name")
    return {"ultimo_pedido": ultimo_pedido, "ultima_fala": ultima_fala, "tool_pendente": tool_pendente}


def coletar(so_workspace=True):
    """Sessões vivas + resumo do que cada uma está fazendo, prontas pra exibir."""
    saida = []
    for s in listar_peers(so_workspace=so_workspace):
        saida.append({**s, **_resumo_transcript(s.get("sessionId", ""))})
    ordem = {"idle": 0, "busy": 1, "shell": 2}
    saida.sort(key=lambda s: ordem.get(s.get("status"), 1))
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


def ha_quanto(sessao):
    inicio = sessao.get("startedAt")
    if not inicio:
        return "?"
    return _dur(time.time() - inicio / 1000.0)


def enviar_mensagem_peer(nome, texto):
    """Dispara um `claude -p` descartável que só chama SendMessage(to=nome, message=texto).

    Não é resume da sessão alvo nem toca no `.jsonl` dela diretamente — é outra
    sessão (esta, efêmera) conversando com ela pelo canal de mensagens do próprio
    Claude Code. Por isso não rouba: a sessão alvo recebe como se fosse uma
    mensagem de outra pessoa, processa quando puder, e o dono continua livre pra
    usar o terminal dela a qualquer momento.
    """
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
