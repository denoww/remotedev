# remotedev <!-- v2 -->

Telegram Bot para controle remoto multiprojeto via desktop. Suporta múltiplos bots rodando em paralelo.

## Instalação

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

Passo a passo que o script executa:
1. Pergunta o **nome** do bot (ex: `dev`, `prod`)
2. Pede o **TOKEN** (que você copia do [@BotFather](https://t.me/BotFather))
3. Descobre o **CHAT_ID** — basta mandar uma mensagem pro bot no Telegram
4. Salva tudo no `~/.bashrc` e cria o serviço systemd
5. Inicia o bot automaticamente

Para adicionar outro bot, rode `./bot.sh install` novamente com outro nome.

## Gerenciar bots

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

## Comandos disponíveis no Telegram

| Comando | Descrição |
|---------|-----------|
| `/start` | Menu de ajuda |
| `/p` | Trocar projeto (botões) |
| `/p nome` | Trocar projeto direto |
| `/bash comando` | Executa qualquer comando |
| `/claude prompt` | Claude Code no projeto |
| `/git args` | Git (pull, push, status...) |
| `/rails args` | Rails runner/console |
| `/rake task` | Rake task |
| `/log N` | Últimas N linhas do log |
| `/ping` | Verifica se desktop está online |
| `/id` | Mostra seu chat_id |
| `/restart` | Reinicia o bot |
