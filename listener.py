import os
import sys
import json
import subprocess
import urllib.request
import urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = "8430921727"

if not TOKEN:
    raise RuntimeError("Set the TELEGRAM_BOT_TOKEN environment variable first.")

COMMANDS = {
    "run": ["bot.py"],
    "update": ["fo_contract_changes.py"],
    "actions": ["corporate_actions.py"],
    "all": ["bot.py", "fo_contract_changes.py", "corporate_actions.py"],
}

MENU = (
    "Send one of these:\n"
    "run - F&O ban list\n"
    "update - F&O contract changes\n"
    "actions - corporate actions\n"
    "all - run everything"
)


def api(method, timeout=70, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
        return json.loads(r.read().decode())


def send(text):
    api("sendMessage", chat_id=CHAT_ID, text=text[:4000])


def run_script(script):
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}
    try:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=180, env=env,
        )
        output = (result.stdout + result.stderr).strip() or "(no output)"
        status = "OK" if result.returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        output, status = "Timed out after 180 seconds.", "FAILED"
    send(f"[{status}] {script}\n\n{output[-3500:]}")


def main():
    offset = None
    print("Bot is listening... press Ctrl+C to stop.")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            updates = api("getUpdates", **params).get("result", [])
        except Exception as e:
            print("Polling error:", e)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                continue  # ignore everyone except you

            text = (msg.get("text") or "").strip().lower().lstrip("/")
            word = text.split("@")[0].split(" ")[0]

            if word in COMMANDS:
                send(f"Running {word}...")
                for script in COMMANDS[word]:
                    run_script(script)
            else:
                send(MENU)


if __name__ == "__main__":
    main()
