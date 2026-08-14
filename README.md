# BVB Supercup ticket monitor

Watches ticket pages for **Borussia Dortmund vs Bayern Munich, Franz Beckenbauer
Supercup, 22 Aug 2026** and pings your phone when a page shows tickets / changes.

The official shop is BVB's (`ticket-onlineshop.com`). Face-value tickets sell out
in presale, but BVB releases season-ticket-holder returns on their secondary
market — so the realistic win here is catching a **returned ticket** fast.

## Quick start (run on your own machine)

1. Install Python 3.10+, then:
   ```bash
   pip install -r requirements.txt
   ```

2. Set up notifications (pick one — ntfy is easiest):
   - **ntfy:** install the free *ntfy* app (iOS/Android), pick any hard-to-guess
     topic name (e.g. `bvb-supercup-7hk3-alerts`), and in the app "Subscribe" to
     it. Then set it as an env var:
     ```bash
     export NTFY_TOPIC="bvb-supercup-7hk3-alerts"
     ```
   - **Telegram:** message @BotFather -> `/newbot` -> copy the token. Get your
     chat id (message @userinfobot). Then:
     ```bash
     export TELEGRAM_BOT_TOKEN="123456:ABC..."
     export TELEGRAM_CHAT_ID="987654321"
     ```

3. Test it:
   ```bash
   python ticket_monitor.py --test     # you should get a push
   ```

4. Run it:
   ```bash
   python ticket_monitor.py            # loops every 3 min (edit INTERVAL_SECONDS)
   ```

## Important: point it at the right URL

Open the BVB ticket shop in your browser, navigate to the Supercup match, and
copy the address bar. Paste that into the `TARGETS` list at the top of
`ticket_monitor.py` as the `url`. The more specific the page, the better the
signal.

## The bot-protection catch (read this)

Ticket sites block plain scripts (the BVB shop returns HTTP 403 to a basic
request). Two options:

- **Recommended:** flip `USE_PLAYWRIGHT = True` in the script, then:
  ```bash
  pip install playwright && playwright install chromium
  ```
  This drives a real headless browser and is far more likely to get through.
- If a page still blocks you, use `mode: "change"` (the default) on a lighter
  page, or watch a secondary marketplace listing instead.

Keep `INTERVAL_SECONDS` at 120-300. Hammering the site gets your IP blocked and
is rude to the server.

## Run it 24/7 without leaving your computer on

`.github/workflows/monitor.yml` runs the check every 10 min on GitHub's free
runners:

1. Push this folder to a **public** GitHub repo (public = unlimited free minutes).
2. Repo -> Settings -> Secrets and variables -> Actions -> add `NTFY_TOPIC`
   (and/or the Telegram/email secrets). **Never commit tokens into the code.**
3. The workflow runs automatically; trigger a manual run from the Actions tab to
   confirm.

Note: GitHub cron has a 5-min floor and can be delayed under load. For fastest
returns-catching, a small always-on box (Raspberry Pi / cheap VPS) running the
loop with Playwright is the most reliable.

## Modes

- `change` (default): alerts on ANY meaningful change to the page. Best when you
  don't know the exact "available" wording.
- `keyword`: alerts when buy-words appear and sold-out-words are gone. Set the
  keyword lists per target.
