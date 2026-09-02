# remotedev

Bot Telegram para controle remoto de projetos no desktop. Permite executar comandos, interagir com Claude Code, e fazer operações git — tudo pelo Telegram.

## Estrutura

- `remotedev.py` — Handlers do bot (comandos, mensagens, callbacks)
- `lib/config.py` — Config: tokens, projetos (auto-descobertos em ~/workspace), constantes
- `lib/claude.py` — Integração com Claude Code (sessões por chat, execução em stream, progresso ao vivo no Telegram)
- `lib/git_ops.py` — Operações git (diff, push, branch, reset, commit com IA)
- `lib/hooks.py` — Hooks pós-push
- `lib/novo_projeto.py` — Criação de projetos vinext (scaffold, GitHub, config)
- `lib/excluir_projeto.py` — Exclusão de projeto (lista, confirma, remove; nunca o próprio remotedev)
- `lib/media_groups.py` — Agrupa anexos do mesmo álbum do Telegram num único prompt
- `lib/ngrok.py` — Comando /ngrok: detecta a porta do projeto e gerencia túneis
- `lib/users.py` — Gerenciamento de usuários autorizados (persistência JSON, handlers, helpers)
- `lib/utils.py` — Utilitários (estado, autorização, execução de comandos)
- `bot.sh` — Script de gerenciamento (install/uninstall/restart/logs)

## Como rodar

```bash
source venv/bin/activate
python3 remotedev.py <nome_bot>       # ex: python3 remotedev.py dev
```

Precisa das env vars: `TELEGRAM_BOT_<NOME>_TOKEN` e `TELEGRAM_<NOME>_CHAT_ID`.

Em produção roda como systemd user service (`remotedev-<nome>`).

## Convenções

- Código e comentários em português
- Commits em português, gerados por IA quando sem mensagem explícita
- Projetos são auto-descobertos de ~/workspace (qualquer diretório não-oculto)
- Cada chat pode ter um projeto ativo por vez (estado em memória)
- Bot roda em produção — testar antes de alterar handlers ou config
- **Este diretório é o deploy.** Os services systemd rodam direto de
  `~/workspace/remotedev`; não há build nem cópia. Editar um arquivo aqui já muda o
  que o próximo start vai carregar, e o `git pull --ff-only` do `ExecStartPre` só
  sincroniza com o remoto — não protege de edição local em andamento. Consequência:
  se um bot reiniciar no meio de uma edição (reboot, `Restart=always` após queda),
  ele sobe com código pela metade. Ao mexer em `remotedev.py` ou `lib/`, deixe o
  arquivo sempre num estado que compila (`venv/bin/python3 -m py_compile`) antes de
  parar o trabalho.
- Ao adicionar/remover comandos: atualizar `BOTFATHER_COMMANDS` em `lib/config.py` (fonte única) e o `README.md`

## Debugging

- Ao receber um erro, sempre analisar os logs antes de diagnosticar:
  - Serviço (journald): `journalctl --user -u remotedev-<bot> -n 50 --no-pager` (onde `<bot>` é o BOT_NOME do bot atual, ex: `dev`)
  - Claude: `tail -50 claude-<bot>.log`
