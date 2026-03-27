# remotedev <!-- v5 -->

Telegram Bot para controle remoto multiprojeto via desktop. Suporta múltiplos bots rodando em paralelo.

## Instale Python (se não tiver)

```bash
sudo apt install python3-venv   # se necessário
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Instalar um bot

O script interativo pede nome, token e descobre o CHAT_ID automaticamente:

```bash
./bot.sh install
```


## Comandos disponíveis no Telegram

Vá no Telegram e digite `/start` ou `/` para ver os comandos. Eles são registrados automaticamente ao iniciar o bot.

## Gerenciar bots (comandos no computador)

```bash
./bot.sh list                    # lista bots instalados
./bot.sh status                  # status de todos os bots
./bot.sh restart                 # reinicia um bot
./bot.sh stop                    # para um bot
./bot.sh start                   # inicia um bot
./bot.sh logs                    # logs do serviço
./bot.sh logs-claude             # logs do Claude
./bot.sh logs-claude BOT PROJETO # filtra por projeto
./bot.sh uninstall               # remove um bot
```

Todos os comandos que recebem nome do bot são opcionais — se não passar, ele lista os bots e pergunta qual.
