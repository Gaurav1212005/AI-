
import os
import csv
import io
import json
import urllib.request
import urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = "8430921727"

NSE_CSV_URL = "https://archives.nseindia.com/content/fo/fo_secban.csv"

if not TOKEN:
    raise RuntimeError("Telegram token secret is missing.")


def fetch_ban_list():
    request = urllib.request.Request(
        NSE_CSV_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.nseindia.com/"
        }
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()

    if not content or b"<html" in content[:1000].lower():
        raise RuntimeError("NSE did not return a valid CSV file.")

    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    if not rows:
        raise RuntimeError("The NSE CSV is empty.")

    # Column A, Row 1: heading from the CSV
    heading = rows[0][0].strip() if rows[0] else ""

    if not heading:
        raise RuntimeError("Could not find the heading in Column A, Row 1.")

    # Column B, Row 2 onward: stock symbols
    symbols = [
        row[1].strip().upper()
        for row in rows[1:]
        if len(row) > 1 and row[1].strip()
    ]

    if not symbols:
        raise RuntimeError("No securities found in Column B.")

    return symbols, heading


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message.")


try:
    symbols, heading = fetch_ban_list()

    listing = "\n".join(f"• {symbol}" for symbol in symbols)

    message = (
        "📊 NSE F&O BAN MONITOR\n\n"
        f"{heading}\n\n"
        f"{listing}\n\n"
        f"Official NSE CSV:\n{NSE_CSV_URL}\n\n"
        f"Total securities: {len(symbols)}"
    )

    send_telegram(message)

    print("Success: Telegram message sent.")
    print("Heading:", heading)
    print("Extracted securities:", symbols)
    print("Total securities:", len(symbols))

except Exception as error:
    print(f"ERROR: {error}")
    raise
