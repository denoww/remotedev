"""Cliente do agente `pcmux agent --stdio` (JSON-lines) — local ou em outro PC via ssh.

O agente é um conjunto FECHADO de operações (list/preview/send/tecla…): sem shell,
sem custo de modelo, instantâneo. Remoto = o `ssh` do sistema com os argumentos de
`pcremote.sh ssh-args <pc>` (chave de host fixada). Tudo aqui devolve (ok, dado_ou_erro)
e NUNCA levanta: o chamador cai no caminho antigo (SendMessage) se o agente não estiver disponível.
"""
import json
import os
import shutil
import subprocess

PCMUX_BIN = os.environ.get("PCMUX_BIN") or shutil.which("pcmux") or os.path.expanduser("~/.local/bin/pcmux")
PCREMOTE_SH = os.environ.get("PCREMOTE_SH") or os.path.expanduser("~/workspace/pcremote/pcremote.sh")
TIMEOUT = 15


def _argv(pc):
    """None = este PC; senão o argv do ssh (chave de host fixada) que roda o agente lá."""
    if not pc:
        return [PCMUX_BIN, "agent", "--stdio"]
    res = subprocess.run(["bash", PCREMOTE_SH, "ssh-args", pc], capture_output=True, timeout=TIMEOUT)
    args = [a.decode() for a in res.stdout.split(b"\0") if a]
    if res.returncode != 0 or not args:
        raise OSError("ssh-args falhou para " + pc)
    return ["ssh", *args, "~/.local/bin/pcmux agent --stdio"]


def chamar(req, pc=None):
    """Uma operação: (True, resposta_dict) ou (False, mensagem). Abre e fecha o agente a cada chamada."""
    try:
        entrada = json.dumps({"id": 1, **req}) + "\n"
        res = subprocess.run(_argv(pc), input=entrada, capture_output=True, text=True, timeout=TIMEOUT)
        linhas = [l for l in res.stdout.splitlines() if l.strip()]
        for l in linhas:
            r = json.loads(l)
            if r.get("id") == 1:
                return (True, r) if r.get("ok") else (False, r.get("erro") or "erro do agente")
        return False, "agente sem resposta"
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return False, str(e)


def disponivel(pc=None):
    ok, r = chamar({"op": "hello"}, pc)
    return ok and r.get("versao", 0) >= 2


def listar(pc=None):
    """(True, [sessões]) — painel não medido vira erro, NUNCA lista vazia."""
    ok, r = chamar({"op": "list"}, pc)
    if not ok:
        return False, r
    if r.get("painel_ok") is False:
        return False, "painel de sessões não medido"
    return True, r.get("sessoes") or []


def enviar(janela, texto, pc=None):
    return chamar({"op": "send", "janela": janela, "texto": texto}, pc)


def previa(janela, pc=None, linhas=0):
    ok, r = chamar({"op": "preview", "janela": janela, "linhas": linhas}, pc)
    return (True, r.get("preview", "")) if ok else (False, r)


TECLAS = ("Enter", "Escape", "1", "2", "3", "4", "Up", "Down", "Tab")


def tecla(janela, t, pc=None):
    """Só para sessão que PEDE ação (o agente confere de novo na hora)."""
    return chamar({"op": "tecla", "janela": janela, "texto": t}, pc)
