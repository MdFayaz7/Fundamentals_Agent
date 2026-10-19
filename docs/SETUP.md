# Setup on Raspberry Pi 5

A Telegram chat analyst: forward result headlines to the bot, then ask questions about them.

## 1. Prepare the Pi
Install **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager (enable SSH and Wi-Fi there).
Then on the Pi:
```bash
sudo apt-get update && sudo apt-get install -y python3-venv git
```
No ports need to be opened — the agent only makes outgoing connections.

## 2. Copy the project
```bash
git clone <your-repo-url> ~/resultagent   # or: scp -r "Result Agent" pi@<pi-ip>:~/resultagent
cd ~/resultagent
```

## 3. Create your Telegram bot
1. In Telegram, open **@BotFather** → `/newbot` → pick a name → copy the **token**.
2. Open your new bot and send it any message (e.g. "hi").

## 4. AI keys
At least one free key: **Groq** (https://console.groq.com). Optional fallbacks: GEMINI_API_KEY,
MISTRAL_API_KEY, OPENROUTER_API_KEY — the bot switches automatically if one fails.

## 5. Install
```bash
./deploy/install.sh          # first run creates .env and stops
nano .env                    # paste TELEGRAM_BOT_TOKEN (and optional GROQ_API_KEY)
.venv/bin/python -m resultagent get-chat-id    # prints your chat_id
nano .env                    # paste TELEGRAM_CHAT_ID
.venv/bin/python -m resultagent test-telegram  # you should get "Hello from Results Agent"
./deploy/install.sh          # installs + starts the service
```

## 6. Use it
In Telegram, send `/help` to your bot. Forward result posts, then ask questions.
```bash
journalctl -u resultagent -f        # live log
```

## Update
```bash
cd ~/resultagent && git pull && .venv/bin/pip install -q . && sudo systemctl restart resultagent
```
