# RodrigoDevBot

Telegram Bot para controle remoto multiprojeto via desktop. Suporta múltiplas instâncias (ex: dev, prod) rodando em paralelo.

## Instalação

Caso não tenha o `python3-venv` instalado:

```bash
sudo apt install python3-venv
```

Crie um ambiente virtual e instale as dependências:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Setup de um bot

Cada bot precisa de um nome (ex: `dev`, `prod`). Os exemplos abaixo usam `dev`.

### 1. Criar o bot no Telegram

Abra o Telegram, fale com [@BotFather](https://t.me/BotFather) e envie `/newbot`. Siga as instruções e copie o **TOKEN** que ele gerar.

### 2. Salvar o TOKEN no bashrc

Substitua `SEU_TOKEN` e rode:

```bash
MEU_TOKEN="SEU_TOKEN"
NOME="DEV"
grep -q "TELEGRAM_BOT_${NOME}_TOKEN" ~/.bashrc && sed -i "s|export TELEGRAM_BOT_${NOME}_TOKEN=.*|export TELEGRAM_BOT_${NOME}_TOKEN=\"$MEU_TOKEN\"|" ~/.bashrc || echo "export TELEGRAM_BOT_${NOME}_TOKEN=\"$MEU_TOKEN\"" >> ~/.bashrc
source ~/.bashrc
```

### 3. Descobrir seu CHAT_ID

```bash
python3 telegram_desktop_bot.py dev --get-chat-id
```

Abra o Telegram e mande **qualquer mensagem** pro seu bot. O terminal vai mostrar seu `CHAT_ID`. Copie-o.

### 4. Salvar o CHAT_ID no bashrc

Substitua `SEU_CHAT_ID` e rode:

```bash
MEU_CHAT_ID="SEU_CHAT_ID"
NOME="DEV"
grep -q "TELEGRAM_${NOME}_CHAT_ID" ~/.bashrc && sed -i "s|export TELEGRAM_${NOME}_CHAT_ID=.*|export TELEGRAM_${NOME}_CHAT_ID=\"$MEU_CHAT_ID\"|" ~/.bashrc || echo "export TELEGRAM_${NOME}_CHAT_ID=\"$MEU_CHAT_ID\"" >> ~/.bashrc
source ~/.bashrc
```

### 5. Registrar comandos no Telegram

Fale com [@BotFather](https://t.me/BotFather), envie `/setcommands`, selecione seu bot e cole:

```
start - Menu de ajuda
p - Trocar projeto
bash - Executar comando no terminal
claude - Claude Code no projeto
git - Comandos git
rails - Rails runner/console
rake - Rake task
log - Ultimas linhas do log
ping - Verifica se desktop esta online
id - Mostra seu chat_id
restart - Reinicia o bot
```

### 6. Instalar como serviço (inicia com o sistema)

```bash
./install_service.sh dev
```

Depois abra seu bot no Telegram e envie `/start`.

## Adicionar outro bot

Repita os passos acima com outro nome. Exemplo para `prod`:

```bash
# Passo 2 — TOKEN
MEU_TOKEN="TOKEN_DO_BOT_PROD"
NOME="PROD"
grep -q "TELEGRAM_BOT_${NOME}_TOKEN" ~/.bashrc && sed -i "s|export TELEGRAM_BOT_${NOME}_TOKEN=.*|export TELEGRAM_BOT_${NOME}_TOKEN=\"$MEU_TOKEN\"|" ~/.bashrc || echo "export TELEGRAM_BOT_${NOME}_TOKEN=\"$MEU_TOKEN\"" >> ~/.bashrc
source ~/.bashrc

# Passo 3 — CHAT_ID
python3 telegram_desktop_bot.py prod --get-chat-id

# Passo 4 — Salvar CHAT_ID
MEU_CHAT_ID="CHAT_ID_DO_PROD"
NOME="PROD"
grep -q "TELEGRAM_${NOME}_CHAT_ID" ~/.bashrc && sed -i "s|export TELEGRAM_${NOME}_CHAT_ID=.*|export TELEGRAM_${NOME}_CHAT_ID=\"$MEU_CHAT_ID\"|" ~/.bashrc || echo "export TELEGRAM_${NOME}_CHAT_ID=\"$MEU_CHAT_ID\"" >> ~/.bashrc
source ~/.bashrc

# Passo 6 — Instalar serviço
./install_service.sh prod
```

## Logs

```bash
./logs_do_service.sh dev        # logs do bot dev
./logs_do_service.sh prod       # logs do bot prod
./logs_do_claude.sh dev         # execuções do /claude no bot dev
./logs_do_claude.sh dev scsip   # filtra por projeto
```

Status:

```bash
systemctl --user status rodrigodevbot-dev
systemctl --user status rodrigodevbot-prod
```

## Desinstalar

```bash
./uninstall_service.sh dev
./uninstall_service.sh prod
```

## Comandos disponíveis

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
