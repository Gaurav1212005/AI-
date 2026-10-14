import os
import sys
import json
import time
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

============================================================

CONFIGURATION

============================================================

TOKEN = os.environ.get(“TELEGRAM_BOT_TOKEN”, “”).strip()
CHAT_ID = str(
os.environ.get(“TELEGRAM_CHAT_ID”, “8430921727”)
).strip()

BASE_DIR = os.path.dirname(os.path.abspath(file))
SCRIPT_TIMEOUT = 180
MESSAGE_LIMIT = 3900
POLL_TIMEOUT = 50

COMMANDS = {
“run”: {
“scripts”: [“bot.py”],
“description”: “NSE F&O ban list”,
},
“update”: {
“scripts”: [“fo_contract_changes.py”],
“description”: “NSE F&O contract changes”,
},
“actions”: {
“scripts”: [“corporate_actions.py”],
“description”: “Corporate actions”,
},
“all”: {
“scripts”: [
“bot.py”,
“fo_contract_changes.py”,
“corporate_actions.py”,
],
“description”: “All monitoring programs”,
},
}

MENU = (
“🤖 INTERACTIVE NSE MONITOR\n”
“━━━━━━━━━━━━━━━━━━━━\n\n”
“Available commands:\n\n”
“/run — F&O ban list\n”
“/update — F&O contract changes\n”
“/actions — Corporate actions\n”
“/all — Run everything\n”
“/status — Listener status\n”
“/help — Show this menu\n\n”
“You can also send commands without /.”
)

execution_lock = threading.Lock()
START_TIME = datetime.now(timezone.utc)

============================================================

TELEGRAM API

============================================================

def api(method, timeout=60, **params):
“”“Make a request to the Telegram Bot API.”””

url = f"https://api.telegram.org/bot{TOKEN}/{method}"
data = urllib.parse.urlencode(params).encode("utf-8")
request = urllib.request.Request(
    url,
    data=data,
    headers={
        "Content-Type": "application/x-www-form-urlencoded"
    },
)
with urllib.request.urlopen(request, timeout=timeout) as response:
    result = json.loads(response.read().decode("utf-8"))
if not result.get("ok"):
    raise RuntimeError(
        result.get("description", "Telegram API request failed")
    )
return result

def send(message):
“”“Send a Telegram message, splitting long reports if necessary.”””

message = str(message).strip()
if not message:
    return
while message:
    if len(message) <= MESSAGE_LIMIT:
        chunk = message
        message = ""
    else:
        split_at = message.rfind("\n", 0, MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = MESSAGE_LIMIT
        chunk = message[:split_at]
        message = message[split_at:].lstrip("\n")
    api(
        "sendMessage",
        timeout=30,
        chat_id=CHAT_ID,
        text=chunk,
    )

============================================================

OUTPUT CLEANING

============================================================

def clean_report(output):
“”“Remove known technical logs from captured script output.”””

skip_exact = {
    "Telegram message sent successfully.",
    "Telegram message sent successfully",
    "Corporate-actions monitor completed.",
    "Corporate-actions monitor completed",
    "Starting corporate-actions monitor...",
    "Starting corporate-actions monitor...",
    "Connecting to NSE corporate-actions data...",
    "NSE returned zero corporate-action records.",
}
skip_prefixes = (
    "[OK]",
    "Success: Telegram message sent",
    "Heading:",
    "Extracted securities:",
    "DEBUG:",
    "Debug:",
    "DEBUG ",
    "Debug ",
    "Watchlist companies:",
    "NSE page status:",
    "Corporate-actions status:",
    "NSE records received:",
    "Matching watchlist actions:",
)
cleaned = []
for line in output.splitlines():
    stripped = line.strip()
    if not stripped:
        cleaned.append("")
        continue
    if stripped in skip_exact:
        continue
    if any(
        stripped.startswith(prefix)
        for prefix in skip_prefixes
    ):
        continue
    cleaned.append(line)
report = "\n".join(cleaned).strip()
while "\n\n\n" in report:
    report = report.replace("\n\n\n", "\n\n")
return report

============================================================

PROGRAM EXECUTION

============================================================

def run_script(script):
“”“Run one Python script and return its report.”””

script_path = os.path.join(BASE_DIR, script)
if not os.path.isfile(script_path):
    return False, f"❌ Required file not found: {script}"
env = os.environ.copy()
env["TELEGRAM_BOT_TOKEN"] = TOKEN
env["TELEGRAM_CHAT_ID"] = CHAT_ID
# Prevent corporate_actions.py from sending its own message.
# The listener will send the captured report instead.
if script == "corporate_actions.py":
    env["LISTENER_MODE"] = "1"
else:
    env.pop("LISTENER_MODE", None)
try:
    result = subprocess.run(
        [sys.executable, script_path],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT,
        env=env,
    )
    # For successful execution, use stdout as the report.
    # Do not append stderr logs to the report.
    if result.returncode == 0:
        report = clean_report(result.stdout)
        return True, report
    # Only include captured error details if execution failed.
    error_output = "\n".join(
        part.strip()
        for part in (result.stderr, result.stdout)
        if part.strip()
    )
    error = clean_report(error_output)
    return False, (
        f"❌ {script} failed.\n\n"
        f"{error[-3000:] or 'No error details available.'}"
    )
except subprocess.TimeoutExpired:
    return False, (
        f"❌ {script} timed out after "
        f"{SCRIPT_TIMEOUT} seconds."
    )
except Exception as exc:
    return False, f"❌ Could not execute {script}: {exc}"

def execute_command(command):
“”“Execute the programs associated with a Telegram command.”””

if not execution_lock.acquire(blocking=False):
    send(
        "⏳ Another task is already running. "
        "Please wait for it to finish."
    )
    return
try:
    config = COMMANDS[command]
    failures = 0
    for script in config["scripts"]:
        success, report = run_script(script)
        if not success:
            failures += 1
            send(report)
            continue
        # Send only the actual report.
        if report:
            send(report)
    if failures:
        send(
            f"⚠️ Finished with {failures} failed program(s)."
        )
finally:
    execution_lock.release()

============================================================

COMMAND HANDLING

============================================================

def get_command(message):
“”“Normalize commands with or without a leading slash.”””

text = (message.get("text") or "").strip()
if not text:
    return ""
command = text.split()[0]
command = command.split("@")[0]
return command.lstrip("/").lower()

def handle_message(message):
“”“Handle messages only from the authorized Telegram chat.”””

chat = message.get("chat", {})
incoming_chat_id = str(chat.get("id", ""))
if incoming_chat_id != CHAT_ID:
    return
command = get_command(message)
if command in ("start", "help", "menu"):
    send(MENU)
    return
if command == "status":
    uptime = datetime.now(timezone.utc) - START_TIME
    total_minutes = int(uptime.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    send(
        "✅ LISTENER STATUS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Listener: Running\n"
        f"Uptime: {hours}h {minutes}m\n"
        "Execution: Sequential\n\n"
        "This checks the listener, not NSE availability."
    )
    return
if command in COMMANDS:
    execute_command(command)
    return
send("❓ Unknown command.\n\nSend /help to see the menu.")

============================================================

MAIN LISTENER

============================================================

def main():
if not TOKEN:
raise RuntimeError(
“TELEGRAM_BOT_TOKEN is missing. “
“Set it as an environment variable.”
)

bot_info = api("getMe", timeout=20)
bot_name = bot_info["result"].get("username", "unknown")
print(f"Connected to Telegram bot: @{bot_name}")
print("Listener is running. Press Ctrl+C to stop.")
offset = None
while True:
    try:
        params = {}
        if offset is not None:
            params["offset"] = offset
        response = api(
            "getUpdates",
            timeout=POLL_TIMEOUT + 10,
            allowed_updates=json.dumps(["message"]),
            **params,
        )
        for update in response.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if not message:
                continue
            try:
                handle_message(message)
            except Exception as exc:
                # Keep technical details in Terminal only.
                print("Message handling error:", exc)
                if str(
                    message.get("chat", {}).get("id", "")
                ) == CHAT_ID:
                    try:
                        send(
                            "❌ The command could not be completed. "
                            "Check Terminal for details."
                        )
                    except Exception:
                        pass
    except KeyboardInterrupt:
        print("Listener stopped.")
        break
    except urllib.error.HTTPError as exc:
        print("Telegram HTTP error:", exc.code)
        time.sleep(10)
    except Exception as exc:
        print("Polling error:", exc)
        time.sleep(5)

if name == “main”:
main()
