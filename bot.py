
import os
import csv
import io
import json
import urllib.request
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

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

    header = [cell.strip().lower() for cell in rows[0]]

    symbol_col = next(
        (
            i for i, name in enumerate(header)
            if any(word in name for word in
                   ("symbol", "security", "underlying", "stock"))
        ),
        None
    )

    start = 1 if symbol_col is not None else 0

    if symbol_col is None:
        # Some CSV files may not have a header.
        symbol_col = 0

    symbols = []
    for row in rows[start:]:
        if len(row) <= symbol_col:
            continue

        symbol = row[symbol_col].strip().upper()

        if not symbol or symbol in ("SYMBOL", "SECURITY", "UNDERLYING"):
            continue

        symbols.append(symbol)

    symbols = sorted(set(symbols))

    if not symbols:
        raise RuntimeError(
            "No securities extracted. Check the NSE CSV format."
        )

    return content, symbols

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
    csv_content, symbols = fetch_ban_list()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y")

    listing = "\n".join(f"• {symbol}" for symbol in symbols)

    message = (
        "📊 NSE F&O BAN MONITOR\n"
        f"Checked: {today}\n\n"
        "SECURITIES IN BAN PERIOD\n\n"
        f"{listing}\n\n"
        f"Official NSE CSV:\n{NSE_CSV_URL}\n\n"
        f"Total securities: {len(symbols)}\n"
        "Source: NSE India"
    )

    send_telegram(message)
    print(f"Success: Telegram message sent. Securities found: {len(symbols)}")
    print("Extracted securities:", symbols)

except Exception as error:
    print(f"ERROR: {error}")
    raise
