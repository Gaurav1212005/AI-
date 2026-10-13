
import os
import json
import urllib.request
import urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = "8430921727"

if not TOKEN:
    raise RuntimeError("Telegram bot token is not configured.")

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
data = urllib.parse.urlencode({
    "chat_id": CHAT_ID,
    "text": "Test successful! Your NSE F&O Ban Monitor is connected. Daily alerts will be configured next."
}).encode()

request = urllib.request.Request(url, data=data)

with urllib.request.urlopen(request, timeout=20) as response:
    result = json.loads(response.read().decode())

if not result.get("ok"):
    raise RuntimeError(f"Telegram API error: {result}")

print("Test message sent successfully.")
