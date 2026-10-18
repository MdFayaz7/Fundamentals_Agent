# Setup on Raspberry Pi 5

The agent watches NSE + BSE 24/7. When a company files quarterly results it reads the PDF,
checks the numbers add up, and sends you a short Telegram report within about a minute.

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

## 4. Get free LLM keys (used to read scanned PDFs + write the "AI view")
- **OpenRouter** (reads scanned result tables): https://openrouter.ai → Keys → create key.
  Strongly recommended: add a one-time **$10 credit** — free models then allow
  **1,000 requests/day instead of 50**. Busy result days have 300+ results.
- **Groq** (AI commentary): https://console.groq.com → API Keys → create key.
- Optional **Gemini** key for extra capacity: https://aistudio.google.com/apikey
  (then uncomment the gemini model block in `config.toml`).

Without any keys the agent still runs: digital PDFs get full reports, scanned ones get a
"results filed — open the PDF" alert.

## 5. Install
```bash
./deploy/install.sh          # first run creates .env and stops
nano .env                    # paste TELEGRAM_BOT_TOKEN, GROQ_API_KEY, OPENROUTER_API_KEY
.venv/bin/python -m resultagent get-chat-id    # prints your chat_id
nano .env                    # paste TELEGRAM_CHAT_ID
.venv/bin/python -m resultagent test-telegram  # you should get "Hello from Results Agent"
./deploy/install.sh          # installs + starts the service
```

## 6. Check it
```bash
journalctl -u resultagent -f        # live log: one line per filing
systemctl status resultagent
```
Every morning at 08:30 IST you get a heartbeat: "Results agent alive · yesterday: N alerts…".
If NSE or BSE stops responding for 10 minutes you get a warning.

Test on a past day without sending anything:
```bash
.venv/bin/python -m resultagent replay --date 2026-10-17
```

## 7. Turn on the company filter (later)
In `config.toml`:
```toml
[filter]
enabled = true
min_mcap_cr = 500                 # only companies above ₹500 Cr market cap
watchlist = ["RELIANCE", "TCS"]   # always alerted
```
Then `sudo systemctl restart resultagent`.

## Update
```bash
cd ~/resultagent && git pull && .venv/bin/pip install -q . && sudo systemctl restart resultagent
```

## Reading an alert
- 🟢/🟡/🔴 + **Verdict & Score (1–10)** — quick read of the quarter.
- **YoY** = vs same quarter last year (most important), **QoQ** = vs previous quarter.
- ✅ / ⚠️ lines explain in plain English what is good or worrying.
- **✔ verified** = numbers read from the PDF and cross-checked (Revenue + Other income = Total
  income, PBT − Tax = PAT…). **✔ AI-read, verified** = read by the vision AI, same checks passed.
- ⚪ "results filed" = the PDF couldn't be read reliably — tap the PDF link.

## For the future algo
Every alert is also stored as JSON in `data/agent.db` table `events`
(`confidence`, `score`, `verdict`, all numbers). Only use `VERIFIED` / `LLM_VERIFIED` rows.
Set `webhook_url` in `config.toml` to receive each event as an HTTP POST.
