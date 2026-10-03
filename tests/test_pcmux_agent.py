"""Teste do cliente do agente com um agente FALSO (sem tmux, sem PC real). Rodar: python3 tests/test_pcmux_agent.py"""
import os, stat, sys, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
d = tempfile.mkdtemp()
fake = os.path.join(d, "pcmux")
open(fake, "w").write('''#!/usr/bin/env python3
import sys, json
for l in sys.stdin:
    r = json.loads(l); op = r["op"]
    if op == "hello": print(json.dumps({"id": r["id"], "ok": True, "versao": 2}))
    elif op == "list": print(json.dumps({"id": r["id"], "ok": True, "painel_ok": r.get("x") is None, "sessoes": [{"janela": "claude:1", "nome": "a", "estado": "precisa"}]}))
    elif op == "preview": print(json.dumps({"id": r["id"], "ok": True, "preview": "tela"}))
    elif op == "tecla": print(json.dumps({"id": r["id"], "ok": False, "erro": "não está pedindo ação"}))
    else: print(json.dumps({"id": r["id"], "ok": True}))
''')
os.chmod(fake, 0o755)
os.environ["PCMUX_BIN"] = fake
from lib import pcmux_agent as p
p.PCMUX_BIN = fake
assert p.disponivel()
ok, ss = p.listar(); assert ok and ss[0]["janela"] == "claude:1", (ok, ss)
assert p.previa("claude:1") == (True, "tela")
ok, e = p.tecla("claude:1", "1"); assert not ok and "ação" in e
ok, e = p.enviar("claude:1", "oi"); assert ok
p.PCMUX_BIN = "/nao/existe"
ok, e = p.listar(); assert not ok and isinstance(e, str)
print("ok")
