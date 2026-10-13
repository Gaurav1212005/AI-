
import json
import os
import re
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
            "watchlist.txt was not found in the repository."
        )

    companies = [
        line.strip()
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]

    if not companies:
        raise ValueError(
            "watchlist.txt is empty."
        )

    return companies


# --------------------------------------------------
# FETCH NSE CORPORATE ACTIONS
# --------------------------------------------------

def fetch_actions():
    print("Connecting to NSE corporate-actions data...")

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

    # Establish an NSE session first.
    request = urllib.request.Request(
        NSE_HOME,
        headers=headers,
    )

    with opener.open(request, timeout=30) as response:
        print("NSE page status:", response.status)

    # Retrieve corporate-action records.
    request = urllib.request.Request(
        NSE_API,
        headers=headers,
    )

    with opener.open(request, timeout=30) as response:
        raw_data = response.read().decode("utf-8")
        print("Corporate-actions status:", response.status)

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

    if not records:
        print("NSE returned zero corporate-action records.")

    return records


# --------------------------------------------------
# MATCH AGAINST WATCHLIST
# --------------------------------------------------

def match_actions(watchlist, actions):
    matched = []
    seen = set()

    for action in actions:
        company_name = normalize(
            action.get("comp", "")
        )

        symbol = normalize(
            action.get("symbol", "")
        )

        for watch_company in watchlist:
            watch_name = normalize(watch_company)

            # Match company-name phrases or exact symbols.
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
                    "company": action.get("comp", watch_company),
                    "symbol": action.get("symbol", "N/A"),
                    "purpose": action.get("subject", "N/A"),
                    "ex_date": action.get("exDate", "N/A"),
                    "record_date": action.get("recDate", "N/A"),
                })
                seen.add(key)

            break

    return matched


# --------------------------------------------------
# SEND TELEGRAM MESSAGE
# --------------------------------------------------

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Telegram credentials are missing. "
            "Check the GitHub Actions environment variables."
        )

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    # Telegram messages have a maximum length.
    # Split long reports into safe-sized messages.
    chunks = []
    remaining = message

    while remaining:
        if len(remaining) <= 3500:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, 3500)

        if split_at <= 0:
            split_at = 3500

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    for chunk in chunks:
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
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
                "Telegram rejected the message."
            )

    print("Telegram message sent successfully.")


# --------------------------------------------------
# BUILD TELEGRAM REPORT
# --------------------------------------------------


def build_report(watchlist, actions, matched):
    lines = ["NSE CORPORATE ACTIONS", ""]

    if not matched:
        lines.append(
            "No matching corporate actions found for your watchlist."
        )
        return "\n".join(lines)

    for item in matched:
        lines.extend([
            f"Company: {item['company']}",
            f"Symbol: {item['symbol']}",
            f"Action: {item['purpose']}",
            f"Ex-date: {item['ex_date']}",
            f"Record date: {item['record_date']}",
            "",
        ])

    return "\n".join(lines)


# --------------------------------------------------
# MAIN PROGRAM
# --------------------------------------------------

def main():
    print("Starting corporate-actions monitor...")

    watchlist = load_watchlist()
    print("Watchlist companies:", len(watchlist))

    actions = fetch_actions()
    print("NSE records received:", len(actions))

    matched = match_actions(watchlist, actions)
    print("Matching watchlist actions:", len(matched))

    for item in matched:
        print(
            json.dumps(
                item,
                ensure_ascii=False,
            )
        )

    report = build_report(
        watchlist,
        actions,
        matched,
    )

    send_telegram(report)

    print("Corporate-actions monitor completed.")


if __name__ == "__main__":
    main()
