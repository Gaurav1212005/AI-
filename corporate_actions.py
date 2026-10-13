
import json
import urllib.request
import http.cookiejar

NSE_HOME = "https://www.nseindia.in/companies-listing/corporate-filings-application"
NSE_API = "https://www.nseindia.com/api/corporates-corporateActions?index=equities"

def main():
    print("Testing connection to NSE corporate-actions data...")

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
        "Referer": NSE_HOME,
    }

    # Visit NSE first to establish a session.
    request = urllib.request.Request(
        NSE_HOME,
        headers=headers
    )

    with opener.open(request, timeout=30) as response:
        print("NSE homepage status:", response.status)

    # Request the corporate-actions data.
    request = urllib.request.Request(
        NSE_API,
        headers=headers
    )

    with opener.open(request, timeout=30) as response:
        raw_data = response.read().decode("utf-8")
        print("Corporate-actions response status:", response.status)

    data = json.loads(raw_data)

    if isinstance(data, dict):
        records = data.get("data", [])
    elif isinstance(data, list):
        records = data
    else:
        records = []

    print("Records received:", len(records))

    for record in records[:5]:
        print(json.dumps(record, ensure_ascii=False))

    if not records:
        raise RuntimeError(
            "No records returned. NSE may have changed its API "
            "or the endpoint may require additional parameters."
        )

    print("NSE corporate-actions connection test passed.")

if __name__ == "__main__":
    main()
