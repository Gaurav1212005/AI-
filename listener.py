import os
import sys
import json
import time
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta

# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = str(
    os.environ.get("TELEGRAM_CHAT_ID", "8430921727")
).strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_TIMEOUT = 180
TELEGRAM_MESSAGE_LIMIT = 3900
POLL_TIMEOUT = 50

COMMANDS = {
    "run": {
        "scripts": ["bot.py"],
        "description": "NSE F&O ban list",
    },
    "update": {
        "scripts": ["fo_contract_changes.py"],
        "description": "NSE F&O contract changes",
    },
    "actions": {
        "scripts": ["corporate_actions.py"],
        "description": "Corporate actions",
    },
    "all": {
        "scripts": [
            "bot.py",
            "fo_contract_changes.py",
            "corporate_actions.py",
        ],
        "description": "Run all three programs",
    },
}

MENU = (
    "🤖 INTERACTIVE NSE MONITOR\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Choose a command:\n\n"
    "/run — F&O ban list\n"
    "/update — F&O contract changes\n"
    "/actions — Corporate actions\n"
    "/all — Run everything\n"
    "/status — Listener status\n"
    "/help — Show this menu\n\n"
    "You can also type commands without /."
)

# Prevent overlapping executions.
execution_lock = threading.Lock()

# Listener start time.
START_TIME = datetime.now(timezone.utc)


# ============================================================
# TELEGRAM API
# ============================================================

def api(method, timeout=60, **params):
    """Call the Telegram Bot API."""

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"

    data = urllib.parse.urlencode(params).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API request failed")
        )

    return result


def send(text):
    """Send a Telegram message, splitting long reports if necessary."""

    text = str(text).strip()

    if not text:
        text = "No report was produced."

    chunks = []

    while len(text) > TELEGRAM_MESSAGE_LIMIT:
        split_at = text.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)

        if split_at < 1:
            split_at = TELEGRAM_MESSAGE_LIMIT

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    if text:
        chunks.append(text)

    for chunk in chunks:
        api(
            "sendMessage",
            chat_id=CHAT_ID,
            text=chunk,
            timeout=30,
        )


# ============================================================
# REPORT CLEANING
# ============================================================

def clean_report(output):
    """
    Remove common debugging and delivery-confirmation lines.

    Preserve the actual report, including security names,
    dates, corporate actions and contract-change details.
    """

    lines = output.splitlines()
    cleaned = []

    skip_prefixes = (
        "[OK]",
        "Success: Telegram message sent.",
        "Success: Telegram message sent",
        "Heading:",
        "Extracted securities:",
        "Total securities:",
        "DEBUG:",
        "Debug:",
        "DEBUG ",
        "Debug ",
    )

    for line in lines:
        stripped = line.strip()

        if any(stripped.startswith(prefix) for prefix in skip_prefixes):
            continue

        # Ignore standalone debugging messages.
        if stripped.lower().startswith("debugging output:"):
            continue

        cleaned.append(line)

    # Remove excess blank lines.
    report = "\n".join(cleaned).strip()

    while "\n\n\n" in report:
        report = report.replace("\n\n\n", "\n\n")

    return report


# ============================================================
# SCRIPT EXECUTION
# ============================================================

def run_script(script):
    """Execute a Python program and return its report."""

    script_path = os.path.join(BASE_DIR, script)

    if not os.path.isfile(script_path):
        return {
            "success": False,
            "script": script,
            "report": f"Required file not found: {script}",
        }

    env = os.environ.copy()

    env["TELEGRAM_BOT_TOKEN"] = TOKEN
    env["TELEGRAM_CHAT_ID"] = CHAT_ID

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env=env,
        )

        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part.strip()
        )

        if result.returncode != 0:
            # Send a useful error if the program fails.
            error_text = clean_report(output)

            return {
                "success": False,
                "script": script,
                "report": (
                    f"❌ {script} failed.\n\n"
                    f"{error_text[-3000:] or 'No error details available.'}"
                ),
            }

        report = clean_report(output)

        return {
            "success": True,
            "script": script,
            "report": report,
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "script": script,
            "report": (
                f"❌ {script} exceeded the "
                f"{SCRIPT_TIMEOUT}-second execution limit."
            ),
        }

    except Exception as exc:
        return {
            "success": False,
            "script": script,
            "report": f"❌ Could not execute {script}: {exc}",
        }


def execute_command(command):
    """Run the program or programs associated with a command."""

    if not execution_lock.acquire(blocking=False):
        send(
            "⏳ Another task is already running.\n"
            "Please wait for it to finish before starting another."
        )
        return

    try:
        config = COMMANDS[command]
        scripts = config["scripts"]

        send(f"⏳ Running: {config['description']}")

        failures = 0

        for script in scripts:
            result = run_script(script)

            if not result["success"]:
                failures += 1

            report = result["report"]

            # Do not send successful execution/debug logs.
            # Send only the report produced by the script.
            if report:
                send(report)
            elif result["success"]:
                # Successful scripts may send their own report
                # directly through the Telegram API.
                # Do not send an extra technical success message.
                pass

        if failures:
            send(
                f"⚠️ Task completed with {failures} "
                f"program failure(s)."
            )

    finally:
        execution_lock.release()


# ============================================================
# COMMAND HANDLING
# ============================================================

def get_command(message):
    """Normalize commands, including optional bot usernames."""

    text = (message.get("text") or "").strip()

    if not text:
        return ""

    first_word = text.split()[0]
    first_word = first_word.split("@")[0]
    first_word = first_word.lstrip("/").lower()

    return first_word


def handle_message(message):
    """Process messages from the authorized Telegram chat only."""

    chat = message.get("chat", {})
    incoming_chat_id = str(chat.get("id", ""))

    # Ignore all other chats.
    if incoming_chat_id != CHAT_ID:
        return

    command = get_command(message)

    if command in ("start", "help", "menu"):
        send(MENU)
        return

    if command == "status":
        uptime = datetime.now(timezone.utc) - START_TIME
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60

        send(
            "✅ LISTENER STATUS\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Listener: Running\n"
            f"Uptime: {hours}h {minutes}m\n"
            f"Available programs: {len(COMMANDS)} commands\n"
            "Execution mode: Sequential\n\n"
            "Note: This confirms the listener is running, "
            "not that NSE data sources are available."
        )
        return

    if command in COMMANDS:
        execute_command(command)
        return

    send(
        "❓ Unknown command.\n\n"
        "Send /help to see the available commands."
    )


# ============================================================
# MAIN POLLING LOOP
# ============================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. "
            "Set it as an environment variable."
        )

    # Check the token before entering the polling loop.
    me = api("getMe", timeout=20)
    bot_name = me["result"].get("username", "unknown")

    print(f"Connected to Telegram bot: @{bot_name}")
    print("Listener is running. Press Ctrl+C to stop.")

    offset = None

    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}

            if offset is not None:
                params["offset"] = offset

            response = api(
                "getUpdates",
                timeout=POLL_TIMEOUT + 10,
                **params,
            )

            for update in response.get("result", []):
                offset = update["update_id"] + 1

                message = (
                    update.get("message")
                    or update.get("edited_message")
                    or {}
                )

                if message:
                    try:
                        handle_message(message)
                    except Exception as exc:
                        print("Message handling error:", exc)

                        # Avoid exposing internal errors or secrets
                        # in Telegram.
                        try:
                            if str(
                                message.get("chat", {}).get("id", "")
                            ) == CHAT_ID:
                                send(
                                    "❌ The command could not be "
                                    "completed. Check the listener "
                                    "logs for details."
                                )
                        except Exception:
                            pass

        except KeyboardInterrupt:
            print("Listener stopped.")
            break

        except urllib.error.HTTPError as exc:
            print("Telegram HTTP error:", exc.code)

            # Avoid repeatedly hammering Telegram after an error.
            time.sleep(10)

        except Exception as exc:
            print("Polling error:", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
