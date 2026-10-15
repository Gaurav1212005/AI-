import html
import json
import os
import re
import textwrap
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

NSE_HOME = (
    "https://www.nseindia.com/"
    "companies-listing/corporate-filings-actions"
)

NSE_API = (
    "https://www.nseindia.com/"
    "api/corporates-corporateActions?index=equities"
)

WATCHLIST_FILE = "watchlist.txt"
TELEGRAM_API = "https://api.telegram.org"

# Set LISTENER_MODE=1 when called by listener.py
LISTENER_MODE = os.environ.get("LISTENER_MODE") == "1"

# Table layout
ACTION_WIDTH = 28          # wrap width of the ACTION column
MAX_MESSAGE_CHARS = 3500   # Telegram limit is 4096


# --------------------------------------------------
# TEXT NORMALIZATION
# --------------------------------------------------

def normalize(text):
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


# --------------------------------------------------
# LOAD WATCHLIST
# --------------------------------------------------

def load_watchlist():
    path = Path(WATCHLIST_FILE)

    if not path.exists():
        raise FileNotFoundError(
            "watchlist.txt was not found."
        )

    companies = [
        line.strip()
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    if not companies:
        raise ValueError("watchlist.txt is empty.")

    return companies


# --------------------------------------------------
# FETCH NSE CORPORATE ACTIONS
# --------------------------------------------------

def fetch_actions():
    cookies = http.cookiejar.CookieJar()

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies)
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
    }

    # Establish an NSE session.
    request = urllib.request.Request(
        NSE_HOME,
        headers=headers,
    )

    with opener.open(request, timeout=30):
        pass

    # Fetch corporate-action records.
    request = urllib.request.Request(
        NSE_API,
        headers=headers,
    )

    with opener.open(request, timeout=30) as response:
        raw_data = response.read().decode("utf-8")

    data = json.loads(raw_data)

    if isinstance(data, dict):
        records = data.get("data", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []

    if not isinstance(records, list):
        raise RuntimeError(
            "Unexpected NSE response format."
        )

    return records


# --------------------------------------------------
# MATCH AGAINST WATCHLIST
# --------------------------------------------------

def match_actions(watchlist, actions):
    matched = []
    seen = set()

    normalized_watchlist = [
        (company, normalize(company))
        for company in watchlist
    ]

    for action in actions:
        company_name = normalize(
            action.get("comp", "")
        )

        symbol = normalize(
            action.get("symbol", "")
        )

        for watch_company, watch_name in normalized_watchlist:

            is_match = (
                len(watch_name) >= 5
                and watch_name in company_name
            ) or (
                watch_name == symbol
            )

            if not is_match:
                continue

            key = (
                symbol,
                normalize(action.get("subject", "")),
                normalize(action.get("exDate", "")),
            )

            if key not in seen:
                matched.append({
                    "watchlist_name": watch_company,
                    "company": action.get(
                        "comp",
                        watch_company,
                    ),
                    "symbol": action.get(
                        "symbol",
                        "N/A",
                    ),
                    "purpose": action.get(
                        "subject",
                        "N/A",
                    ),
                    "ex_date": action.get(
                        "exDate",
                        "N/A",
                    ),
                    "record_date": action.get(
                        "recDate",
                        "N/A",
                    ),
                })

                seen.add(key)

            break

    return matched


# --------------------------------------------------
# BUILD REPORT (table format)
#
# SYMBOL    EX-DATE      REC-DATE     ACTION
# ABC       16-Oct-2026  17-Oct-2026  Dividend - Rs 5
#                                     Per Share
#
# Returns a list of plain-text messages. Each message has the
# title and table header, and is kept under MAX_MESSAGE_CHARS.
# --------------------------------------------------

def clean(value):
    value = str(value or "").strip()
    return value if value and value.upper() != "N/A" else "-"


def build_report_messages(matched):
    title = "NSE CORPORATE ACTIONS"

    if not matched:
        return [
            title
            + "\n\nNo matching corporate actions found "
            "for your watchlist."
        ]

    symbols = [clean(item["symbol"]) for item in matched]
    ex_dates = [clean(item["ex_date"]) for item in matched]
    rec_dates = [clean(item["record_date"]) for item in matched]

    sym_w = max(len("SYMBOL"), max(len(v) for v in symbols))
    ex_w = max(len("EX-DATE"), max(len(v) for v in ex_dates))
    rec_w = max(len("REC-DATE"), max(len(v) for v in rec_dates))

    prefix_w = sym_w + 2 + ex_w + 2 + rec_w + 2

    header = (
        "SYMBOL".ljust(sym_w) + "  "
        + "EX-DATE".ljust(ex_w) + "  "
        + "REC-DATE".ljust(rec_w) + "  "
        + "ACTION"
    )

    # Build one text block per record (action may wrap onto
    # continuation lines aligned under the ACTION column).
    blocks = []
    for item, sym, ex, rec in zip(
        matched, symbols, ex_dates, rec_dates
    ):
        action_lines = textwrap.wrap(
            clean(item["purpose"]),
            width=ACTION_WIDTH,
            break_long_words=True,
        ) or ["-"]

        lines = [
            sym.ljust(sym_w) + "  "
            + ex.ljust(ex_w) + "  "
            + rec.ljust(rec_w) + "  "
            + action_lines[0]
        ]
        for extra in action_lines[1:]:
            lines.append(" " * prefix_w + extra)

        blocks.append("\n".join(lines))

    # Group blocks into messages without splitting a record.
    messages = []
    current = []
    base = f"{title}\n\n{header}\n"
    current_len = len(base)

    for block in blocks:
        if current and current_len + len(block) + 1 > MAX_MESSAGE_CHARS:
            messages.append(base + "\n".join(current))
            current = []
            current_len = len(base)
        current.append(block)
        current_len += len(block) + 1

    if current:
        messages.append(base + "\n".join(current))

    return messages


# --------------------------------------------------
# SEND TELEGRAM MESSAGE
# Sent inside <pre> so the table columns line up.
# --------------------------------------------------

def send_telegram(messages):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Telegram credentials are missing."
        )

    if isinstance(messages, str):
        messages = [messages]

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    for message in messages:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": "<pre>" + html.escape(message) + "</pre>",
            "parse_mode": "HTML",
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                )
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram rejected the message: {result}"
            )


# --------------------------------------------------
# MAIN PROGRAM
# --------------------------------------------------

def main():
    watchlist = load_watchlist()
    actions = fetch_actions()

    matched = match_actions(
        watchlist,
        actions,
    )

    messages = build_report_messages(matched)

    if LISTENER_MODE:
        # listener.py will send this report to Telegram.
        print("\n\n".join(messages))
    else:
        # Standalone or scheduled execution sends it directly.
        send_telegram(messages)


if __name__ == "__main__":
    main()
