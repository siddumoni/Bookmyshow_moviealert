#!/usr/bin/env python3
"""
Booking Watcher — by ayush-cyber01

Keeps an eye on a BookMyShow ticket page and fires off a Telegram alert
the second booking flips open for the movie/theatre/date you've set in
settings.json. A memory.json file remembers the last known state, so
you get exactly one ping on the open transition instead of a flood of
repeat alerts every run.

Every site-specific value (movie, date, theatre) lives in settings.json,
not in this file — so switching targets is a config edit, not a code
change.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = Path(os.environ.get("CONFIG_PATH", BASE_DIR / "config.json"))
MEMORY_FILE = Path(os.environ.get("STATE_PATH", BASE_DIR / "state.json"))

# Browser fingerprint so BookMyShow treats this like a normal Chrome visit
# instead of flagging it as a script.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def read_json(path, fallback=None):
    if not path.exists():
        return fallback
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def get_settings():
    """Load settings.json and layer any GitHub Actions secrets on top."""
    settings = read_json(SETTINGS_FILE, fallback={}) or {}

    secret_overrides = {
        "TARGET_URL": "target_url",
        "THEATRE": "theatre",
        "MOVIE": "movie",
        "REQUESTED_DATE": "requested_date",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_name, settings_key in secret_overrides.items():
        if os.environ.get(env_name):
            settings[settings_key] = os.environ[env_name]

    if os.environ.get("HEADERS_JSON"):
        settings["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # BMS bakes the date into the URL path, so stitch the final page
    # address together from the template + whichever date we're chasing.
    if settings.get("url_template") and settings.get("requested_date"):
        settings["target_url"] = settings["url_template"].format(
            date=settings["requested_date"]
        )

    needed = ["target_url", "telegram_bot_token", "telegram_chat_id"]
    mode = settings.get("detector")
    if mode in ("bms_date", "venue_date"):
        needed.append("requested_date")
    elif mode != "venue_date":
        needed.append("theatre")
    if mode == "venue_date" and not (settings.get("venue_code") or settings.get("venue_codes")):
        sys.exit("venue_date mode needs 'venue_code' or 'venue_codes' set in config.json")

    absent = [key for key in needed if not settings.get(key)]
    if absent:
        sys.exit(f"config.json is missing: {', '.join(absent)}")
    return settings


def notify_telegram(bot_token, chat_id, message):
    endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    response = requests.post(
        endpoint,
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False},
        timeout=30,
    )
    response.raise_for_status()


def grab_page(settings):
    """
    Pull down the target BMS page, routed through an India IP when possible.

    BookMyShow rejects non-India / datacenter traffic outright (GitHub's
    runners get a flat 403), so this goes through one of two workarounds:

    * SCRAPERAPI_KEY — proxies the request via ScraperAPI with
      country_code=in and handles anti-bot checks. Set as a repo secret.
    * PROXY_URL — a plain http(s) proxy string pointed at an India exit.

    With neither secret set, it falls back to a direct request with
    browser-shaped headers plus a homepage warm-up — only reliable if the
    machine running this is itself in India.
    """
    headers = dict(BROWSER_HEADERS)
    headers.update(settings.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        proxied_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {"api_key": scraper_key, "country_code": "in", "url": settings["target_url"]}
        )
        response = requests.get(proxied_url, timeout=90)
        response.raise_for_status()
        return response.text

    proxy_url = os.environ.get("PROXY_URL")
    proxy_config = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    browser_session = requests.Session()
    browser_session.headers.update(headers)

    try:
        browser_session.get("https://in.bookmyshow.com/", timeout=30, proxies=proxy_config)
    except requests.RequestException:
        pass  # warm-up cookie grab failing isn't fatal, just less convincing

    response = browser_session.get(
        settings["target_url"],
        timeout=30,
        proxies=proxy_config,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-chennai"},
    )
    response.raise_for_status()
    return response.text


def check_date_open_anywhere(page_html, settings):
    """
    'Does this date have showtimes at ANY theatre yet' detector.

    BMS quietly swaps in the nearest bookable date when you ask for one
    that isn't live yet, so the date we actually want stays a low-count
    background token (just nav-strip mentions) until it goes live — at
    which point real showtimes render and it becomes the page's single
    most-repeated date.
    """
    target_date = settings["requested_date"]
    min_hits = settings.get("min_references", 10)

    date_tokens = re.findall(r"20\d{6}", page_html)
    if not date_tokens:
        return False

    tally = Counter(date_tokens)
    leading_date, _ = tally.most_common(1)[0]
    target_hits = tally.get(target_date, 0)

    return leading_date == target_date and target_hits >= min_hits


def check_venue_open(page_html, settings):
    """
    'Is THIS theatre bookable on THIS date' detector.

    BMS only renders a venue's booking link (/cinemas/<city>/<slug>/
    buytickets/<code>/<date>) once that venue has live shows for that
    exact date — the date being baked into the link rules out false
    positives from the silent fallback-date behavior above.
    """
    target_date = settings["requested_date"]
    venue_codes = settings.get("venue_codes") or [settings["venue_code"]]
    return any(f"/{code}/{target_date}" in page_html for code in venue_codes)


def check_generic_open(page_html, settings):
    """
    Fallback detector: theatre name + movie name both present, plus a
    'booking is live' phrase, and no 'not yet open' phrase dominating.
    """
    flat_text = re.sub(r"\s+", " ", page_html).lower()

    theatre_name = re.sub(r"\s+", " ", settings["theatre"]).lower().strip()
    if theatre_name not in flat_text:
        return False

    movie_name = settings.get("movie")
    if movie_name and re.sub(r"\s+", " ", movie_name).lower().strip() not in flat_text:
        return False

    open_phrases = settings.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    closed_phrases = settings.get("closed_signals", ["notify me", "coming soon"])

    looks_open = any(phrase.lower() in flat_text for phrase in open_phrases)
    looks_closed_only = any(phrase.lower() in flat_text for phrase in closed_phrases) and not looks_open

    return looks_open and not looks_closed_only


def is_booking_open(page_html, settings):
    mode = settings.get("detector")
    if mode == "venue_date":
        return check_venue_open(page_html, settings)
    if mode == "bms_date":
        return check_date_open_anywhere(page_html, settings)
    return check_generic_open(page_html, settings)


def build_alert_text(settings):
    if settings.get("detector") in ("bms_date", "venue_date"):
        raw_date = settings["requested_date"]
        pretty_date = f"{raw_date[6:8]}-{raw_date[4:6]}-{raw_date[0:4]}"
        venue_bit = settings.get("venue_label") or settings.get("venue_code") or ""
        venue_line = f"📍 {venue_bit}\n" if venue_bit else ""
        return (
            f"🕷️ Tickets are LIVE!\n\n"
            f"{settings.get('movie', 'Movie')}\n"
            f"{venue_line}"
            f"📅 {pretty_date}\n\n"
            f"👉 {settings['target_url']}"
        )
    return (
        f"🎟️ Booking just opened!\n\n"
        f"{settings.get('movie', 'Movie')}\n"
        f"📍 {settings['theatre']}\n\n"
        f"👉 {settings['target_url']}"
    )


def run():
    settings = get_settings()
    memory = read_json(MEMORY_FILE, fallback={"available": False}) or {"available": False}

    watch_label = f"{settings.get('movie', 'target')} :: {settings.get('theatre') or settings.get('requested_date', '?')}"

    try:
        page_html = grab_page(settings)
    except requests.RequestException as err:
        # A blocked/failed fetch shouldn't fail the whole workflow run.
        print(f"[watch] {watch_label} -- fetch failed: {err}")
        return 0

    open_now = is_booking_open(page_html, settings)
    print(f"[watch] {watch_label} -- open_now={open_now} (previously={memory.get('available')})")

    if open_now and not memory.get("available"):
        notify_telegram(
            settings["telegram_bot_token"],
            settings["telegram_chat_id"],
            build_alert_text(settings),
        )
        print(f"[watch] {watch_label} -- alert sent")

    if open_now != memory.get("available"):
        memory["available"] = open_now
        memory["checked_at"] = int(time.time())
        write_json(MEMORY_FILE, memory)

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
