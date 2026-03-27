# RodrigoDevBot

Telegram Bot para controle remoto multiprojeto via desktop.

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

## Setup

### 1. Criar o bot no Telegram

Abra o Telegram, fale com [@BotFather](https://t.me/BotFather) e envie `/newbot`. Siga as instruções e copie o **TOKEN** que ele gerar.

### 2. Salvar o TOKEN no bashrc

Substitua `SEU_TOKEN` pelo token que o BotFather gerou e rode:

```bash
MEU_TOKEN="SEU_TOKEN"
grep -q 'TELEGRAM_BOT_DEV_TOKEN' ~/.bashrc && sed -i "s|export TELEGRAM_BOT_DEV_TOKEN=.*|export TELEGRAM_BOT_DEV_TOKEN=\"$MEU_TOKEN\"|" ~/.bashrc || echo "export TELEGRAM_BOT_DEV_TOKEN=\"$MEU_TOKEN\"" >> ~/.bashrc
source ~/.bashrc
```

### 3. Descobrir seu CHAT_ID

Rode o bot em modo discovery:

```bash
python3 telegram_desktop_bot.py --get-chat-id
```

Abra o Telegram e mande **qualquer mensagem** pro seu bot. O terminal vai mostrar seu `CHAT_ID`. Copie-o.

### 4. Salvar o CHAT_ID no bashrc

Substitua `SEU_CHAT_ID` pelo número que apareceu no terminal e rode:

```bash
MEU_CHAT_ID="SEU_CHAT_ID"
grep -q 'TELEGRAM_DEV_CHAT_ID' ~/.bashrc && sed -i "s|export TELEGRAM_DEV_CHAT_ID=.*|export TELEGRAM_DEV_CHAT_ID=\"$MEU_CHAT_ID\"|" ~/.bashrc || echo "export TELEGRAM_DEV_CHAT_ID=\"$MEU_CHAT_ID\"" >> ~/.bashrc
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
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/rodrigodevbot.service << 'EOF'
[Unit]
Description=RodrigoDevBot - Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/rodrigo/workspace/RodrigoDevBot
ExecStart=/home/rodrigo/workspace/RodrigoDevBot/venv/bin/python3 telegram_desktop_bot.py
Restart=always
RestartSec=5
EnvironmentFile=/home/rodrigo/.config/systemd/user/rodrigodevbot.env

[Install]
WantedBy=default.target
EOF
```

Crie o arquivo de variáveis de ambiente:

```bash
cat > ~/.config/systemd/user/rodrigodevbot.env << EOF
TELEGRAM_BOT_DEV_TOKEN=$TELEGRAM_BOT_DEV_TOKEN
TELEGRAM_DEV_CHAT_ID=$TELEGRAM_DEV_CHAT_ID
EOF
```

Ative e inicie:

```bash
systemctl --user daemon-reload
systemctl --user enable rodrigodevbot
systemctl --user start rodrigodevbot
loginctl enable-linger $USER
```

### 7. Verificar status

```bash
systemctl --user status rodrigodevbot
```

Ver logs:

```bash
journalctl --user -u rodrigodevbot -f
```

Depois abra seu bot no Telegram e envie `/start`.

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
