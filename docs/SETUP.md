# Setup on Raspberry Pi 5

The agent watches NSE + BSE 24/7.
1. Seconds after a company files results you get **⚡ "results out"** with the PDF link.
2. When the company's official **XBRL** data appears on NSE (typically 1–4 hours later) the same
   Telegram message is updated with the **exact, complete report** — no PDF reading, no AI.

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

## 4. (Optional) AI commentary key
A free **Groq** key (https://console.groq.com → API Keys) adds 3–4 plain-English "AI view"
bullets under each report. Everything else works without any API key.

## 5. Install
```bash
./deploy/install.sh          # first run creates .env and stops
nano .env                    # paste TELEGRAM_BOT_TOKEN (and optional GROQ_API_KEY)
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
- ⚡ **results out** — the filing just appeared; tap the PDF for the raw document.
- The message then turns into the report: 🟢/🟡/🔴 **Verdict & Score (1–10)**,
  **YoY** (vs same quarter last year — most important) and **QoQ** (vs previous quarter),
  ✅ / ⚠️ plain-English points and a "What this means" line.
- **✔ exact (exchange XBRL)** — numbers are the company's own filed data.
- "n/a" growth = no meaningful comparison (loss-making base, or the old quarter wasn't filed as XBRL).
- Companies listed only on BSE get the ⚡ alert but no XBRL report yet.

## For the future algo
Every alert is also stored as JSON in `data/agent.db` table `events`
(`source = xbrl`, `score`, `verdict`, all three quarters of numbers).
Set `webhook_url` in `config.toml` to receive each event as an HTTP POST.
