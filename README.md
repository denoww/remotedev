# RodrigoDevBot

Telegram Bot para controle remoto multiprojeto via desktop.

## Instalação

```bash
pip install -r requirements.txt
```

## Setup

1. Fale com [@BotFather](https://t.me/BotFather) no Telegram → `/newbot` → copie o TOKEN
2. Mande `/start` pro bot, depois rode:
   ```bash
   python3 telegram_desktop_bot.py --get-chat-id
   ```
3. Configure `TOKEN` e `CHAT_ID` via variáveis de ambiente ou direto no script
4. Configure seus projetos no dict `PROJETOS`

## Uso

```bash
export TELEGRAM_BOT_TOKEN="seu_token"
export TELEGRAM_CHAT_ID="seu_chat_id"
python3 telegram_desktop_bot.py
```

## Comandos disponíveis

| Comando | Descrição |
|---------|-----------|
| `/start` | Menu de ajuda |
| `/p` | Trocar projeto (botões) |
| `/p erp` | Trocar projeto direto |
| `/bash comando` | Executa qualquer comando |
| `/claude prompt` | Claude Code no projeto |
| `/git args` | Git (pull, push, status...) |
| `/rails args` | Rails runner/console |
| `/rake task` | Rake task |
| `/log N` | Últimas N linhas do log |
| `/ping` | Verifica se desktop está online |
| `/id` | Mostra seu chat_id |
