
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
        raise RuntimeError("The NSE CSV is empty or invalid.")

    header = [cell.strip().lower() for cell in rows[0]]
    symbol_col = next(
        (i for i, name in enumerate(header)
         if "symbol" in name or "security" in name or "underlying" in name),
        0
    )

    start = 1 if any(
        word in " ".join(header)
        for word in ("symbol", "security", "underlying")
    ) else 0

    symbols = sorted({
        row[symbol_col].strip().upper()
        for row in rows[start:]
        if len(row) > symbol_col and row[symbol_col].strip()
    })

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

    if symbols:
        listing = "\n".join(f"• {symbol}" for symbol in symbols)
    else:
        listing = "No securities are listed in the downloaded file."

    message = (
        f"📊 NSE F&O BAN MONITOR\n"
        f"Report checked: {today}\n\n"
        f"Official NSE CSV:\n"
        f"{NSE_CSV_URL}\n\n"
        f"SECURITIES IN BAN PERIOD\n\n"
        f"{listing}\n\n"
        f"Source: NSE India"
    )

    # Telegram messages have a length limit.
    if len(message) > 4000:
        message = message[:3900] + "\n\nList truncated. Check the official CSV."

    send_telegram(message)
    print(f"Success: Telegram message sent. Securities found: {len(symbols)}")

except Exception as error:
    print(f"ERROR: No ban list sent. {error}")
    raise
