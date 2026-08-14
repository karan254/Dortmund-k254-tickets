#!/usr/bin/env python3
"""
BVB Supercup ticket monitor
---------------------------
Watches one or more ticket pages for the Borussia Dortmund vs Bayern Munich
Supercup (22 Aug 2026) and pings you when tickets look available.

Two detection modes per target:
  - "keyword": looks for words that mean "you can buy" and/or the absence of
               "sold out" words. Best when you know what an available page says.
  - "change":  alerts whenever the page's meaningful content changes at all.
               Best when you don't know the exact wording (safe default for the
               BVB / eventim shop, where a returned ticket makes the page change).

Run it two ways:
  python ticket_monitor.py            # loop forever, checking every INTERVAL
  python ticket_monitor.py --once     # single check (use this from cron / GitHub Actions)

Notifications: pick ONE (or more) by filling in the config below.
  - ntfy   : easiest. No account. Install the "ntfy" app, subscribe to a topic,
             set NTFY_TOPIC to that same topic. You'll get a phone push.
  - telegram: create a bot via @BotFather, set token + your chat id.
  - email  : any SMTP account (e.g. a Gmail app password).
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# 1. WHAT TO WATCH
# ----------------------------------------------------------------------------
# Replace the URLs with the exact match/listing pages once you can see them in
# your browser. To get the real URL: open the BVB ticket shop, navigate to the
# Supercup match, and copy the address bar. Paste it in as "url" below.
TARGETS = [
    {
        # The official season-ticket-holder RETURNS market. This is where a
        # sold-out match's freed-up seats appear. This is your primary target.
        "name": "BVB second market",
        "url": "https://www.ticket-onlineshop.com/ols/bvb/de/profis/channel/shop/areaplan/venue/event/676020",
        "mode": "change",
        "available_keywords": ["buy", "available", "verfügbar", "second market", "zweitmarkt"],
        "soldout_keywords": ["no tickets", "keine tickets", "currently no"],
    },
    {
        # The exact Supercup match listing in the shop. REPLACE this URL: open the
        # BVB shop, go to the Dortmund vs Bayern Supercup match, copy the address
        # bar, and paste it here. The more specific, the better the signal.
        "name": "BVB shop – Supercup match",
        "url": "https://www.ticket-onlineshop.com/ols/bvb/de/profis/channel/shop/areaplan/venue/event/676020",
        "mode": "keyword",
        "available_keywords": ["in den warenkorb", "add to cart", "tickets kaufen", "buy tickets"],
        "soldout_keywords": ["ausverkauft", "sold out", "nicht verfügbar", "not available"],
    },
]

# ----------------------------------------------------------------------------
# 2. HOW OFTEN (loop mode only)
# ----------------------------------------------------------------------------
# Be polite: 120-300s is plenty. Hammering the site risks getting your IP blocked.
INTERVAL_SECONDS = 180

# ----------------------------------------------------------------------------
# 3. HOW YOU GET NOTIFIED  (fill in at least one)
# ----------------------------------------------------------------------------
# --- ntfy (recommended, free, no signup) ---
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")          # e.g. "bvb-supercup-8fj3k-alerts"
NTFY_SERVER = "https://ntfy.sh"

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Email (SMTP) ---
EMAIL_ENABLED = False
EMAIL_SMTP_HOST = "smtp.gmail.com"
EMAIL_SMTP_PORT = 587
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")   # app password, not your login
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# ----------------------------------------------------------------------------
# 4. Optional: render JavaScript with a real browser (more reliable, heavier)
# ----------------------------------------------------------------------------
# The BVB/eventim shop loads content with JS AND blocks plain scripts (HTTP 403),
# so a real browser is strongly recommended here. Enable by setting env var
# USE_PLAYWRIGHT=true (the GitHub Actions workflow does this for you), or hardcode
# True below. Locally also run: pip install playwright && playwright install chromium
USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT", "false").lower() in ("1", "true", "yes")

# ----------------------------------------------------------------------------
# 5. "Still no tickets" heartbeat
# ----------------------------------------------------------------------------
# Send a periodic status message (e.g. "No tickets available") so you know the
# monitor is alive. Value is HOURS between heartbeats. 0 = off.
# 24 = once a day. Warning: the monitor checks every 10 min, so a tiny value here
# means dozens of messages a day. Keep it >= 6 unless you really want the spam.
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "0"))

# ----------------------------------------------------------------------------
STATE_FILE = Path(__file__).with_name("state.json")
LOG_FILE = Path(__file__).with_name("monitor.log")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"! could not save state: {exc}")


def fetch(url: str) -> str:
    """Return the visible text of a page, or '' on failure."""
    if USE_PLAYWRIGHT:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()
            return html
        except Exception as exc:  # noqa: BLE001
            log(f"! playwright failed for {url}: {exc}")
            return ""

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log(f"! fetch failed for {url}: {exc}")
        return ""


def normalize(html: str) -> str:
    """Strip tags/scripts/whitespace so we compare meaningful content only."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def check_target(target: dict, state: dict) -> bool:
    """Check one target. Return True if it should trigger a notification."""
    name = target["name"]
    html = fetch(target["url"])
    if not html:
        prev = state.get(name, {})
        prev["status"] = "could not check (site blocked or down)"
        state[name] = prev
        return False

    text = normalize(html)
    prev = state.get(name, {})
    trigger = False
    detail = ""

    if target["mode"] == "keyword":
        has_available = any(k.lower() in text for k in target.get("available_keywords", []))
        has_soldout = any(k.lower() in text for k in target.get("soldout_keywords", []))
        available_now = has_available and not has_soldout
        was_available = prev.get("available", False)
        if available_now and not was_available:
            trigger = True
            detail = "availability keywords appeared"
        state[name] = {"available": available_now, "status": (
            "TICKETS AVAILABLE" if available_now else "no tickets available")}
        log(f"  [{name}] available={available_now} (buy={has_available}, soldout={has_soldout})")

    else:  # change mode
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prev_digest = prev.get("hash")
        status = "no change"
        if prev_digest is None:
            status = "baseline saved"
            log(f"  [{name}] baseline saved (no alert on first run)")
        elif digest != prev_digest:
            trigger = True
            detail = "page content changed"
            status = "page CHANGED - check it"
            log(f"  [{name}] CHANGED")
        else:
            log(f"  [{name}] no change")
        state[name] = {"hash": digest, "status": status}

    if trigger:
        notify(
            title=f"🎟️ BVB Supercup — {name}",
            message=(
                f"{detail}.\nCheck now: {target['url']}\n"
                f"({datetime.now():%Y-%m-%d %H:%M})"
            ),
            url=target["url"],
        )
    return trigger


# --------------------------- notifications ----------------------------------
def notify(title: str, message: str, url: str = "") -> None:
    sent = False
    if NTFY_TOPIC:
        sent |= _notify_ntfy(title, message, url)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        sent |= _notify_telegram(title, message)
    if EMAIL_ENABLED and EMAIL_USERNAME and EMAIL_TO:
        sent |= _notify_email(title, message)
    if not sent:
        log("! ALERT but no notifier configured — set NTFY_TOPIC or Telegram/email.")
        log(f"  {title} :: {message}")


def _notify_ntfy(title: str, message: str, url: str) -> bool:
    try:
        headers = {"Title": title.encode("utf-8")}
        if url:
            headers["Click"] = url
            headers["Actions"] = f"view, Open, {url}"
        r = requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        log("  -> ntfy sent")
        return True
    except requests.RequestException as exc:
        log(f"! ntfy failed: {exc}")
        return False


def _notify_telegram(title: str, message: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n\n{message}"},
            timeout=15,
        )
        r.raise_for_status()
        log("  -> telegram sent")
        return True
    except requests.RequestException as exc:
        log(f"! telegram failed: {exc}")
        return False


def _notify_email(title: str, message: str) -> bool:
    try:
        msg = MIMEText(message)
        msg["Subject"] = title
        msg["From"] = EMAIL_USERNAME
        msg["To"] = EMAIL_TO
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, [EMAIL_TO], msg.as_string())
        log("  -> email sent")
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log(f"! email failed: {exc}")
        return False


# ------------------------------- main ---------------------------------------
def maybe_send_heartbeat(state: dict) -> None:
    """Send a periodic 'still nothing' status message if HEARTBEAT_HOURS is set."""
    if HEARTBEAT_HOURS <= 0:
        return
    now = time.time()
    last = state.get("_heartbeat", 0)
    if now - last < HEARTBEAT_HOURS * 3600:
        return  # not time yet

    lines = []
    for target in TARGETS:
        status = state.get(target["name"], {}).get("status", "unknown")
        lines.append(f"- {target['name']}: {status}")
    summary = "\n".join(lines)
    notify(
        title="⏱️ BVB monitor status",
        message=f"Still watching. Current status:\n{summary}\n({datetime.now():%Y-%m-%d %H:%M})",
    )
    state["_heartbeat"] = now


def run_once() -> None:
    state = load_state()
    log(f"Checking {len(TARGETS)} target(s)...")
    for target in TARGETS:
        try:
            check_target(target, state)
        except Exception as exc:  # noqa: BLE001
            log(f"! error on {target['name']}: {exc}")
    maybe_send_heartbeat(state)
    save_state(state)


def run_loop() -> None:
    log(f"Starting loop, interval={INTERVAL_SECONDS}s. Ctrl+C to stop.")
    while True:
        run_once()
        time.sleep(INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="BVB Supercup ticket monitor")
    parser.add_argument("--once", action="store_true", help="run a single check then exit")
    parser.add_argument("--test", action="store_true", help="send a test notification and exit")
    args = parser.parse_args()

    if args.test:
        notify("✅ Test notification", "Your ticket monitor is wired up correctly.")
        return
    if args.once:
        run_once()
    else:
        try:
            run_loop()
        except KeyboardInterrupt:
            log("Stopped.")
            sys.exit(0)


if __name__ == "__main__":
    main()
