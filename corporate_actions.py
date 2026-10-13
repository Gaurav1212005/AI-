
import json
import re
import urllib.request
import http.cookiejar
from pathlib import Path

NSE_HOME = (
    "https://www.nseindia.in/"
    "companies-listing/corporate-filings-application"
)
NSE_API = (
    "https://www.nseindia.com/"
    "api/corporates-corporateActions?index=equities"
)


def normalize(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_watchlist():
    path = Path("watchlist.txt")

    if not path.exists():
        raise FileNotFoundError("watchlist.txt was not found.")

    companies = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if not companies:
        raise ValueError("watchlist.txt is empty.")

    return companies


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

    request = urllib.request.Request(
        NSE_HOME, headers=headers
    )
    with opener.open(request, timeout=30) as response:
        print("NSE page status:", response.status)

    request = urllib.request.Request(
        NSE_API, headers=headers
    )
    with opener.open(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
        print("Corporate-actions status:", response.status)

    if isinstance(data, dict):
        return data.get("data", [])
    if isinstance(data, list):
        return data
    return []


def main():
    watchlist = load_watchlist()
    actions = fetch_actions()

    print("Watchlist companies:", len(watchlist))
    print("NSE records received:", len(actions))

    matched = []

    for action in actions:
        company = normalize(action.get("comp", ""))
        symbol = normalize(action.get("symbol", ""))

        for watch_company in watchlist:
            watch_name = normalize(watch_company)

            # Match full company-name phrases or exact symbols.
            if (
                len(watch_name) >= 5
                and watch_name in company
            ) or watch_name == symbol:
                matched.append((watch_company, action))
                break

    print("Matching watchlist actions:", len(matched))

    for watch_company, action in matched:
        print(
            json.dumps(
                {
                    "watchlist_name": watch_company,
                    "company": action.get("comp"),
                    "symbol": action.get("symbol"),
                    "purpose": action.get("subject"),
                    "ex_date": action.get("exDate"),
                    "record_date": action.get("recDate"),
                },
                ensure_ascii=False,
            )
        )

    if not matched:
        print(
            "No watchlist matches in the records returned today. "
            "This does not necessarily mean there are no actions "
            "for your companies."
        )


if __name__ == "__main__":
    main()
