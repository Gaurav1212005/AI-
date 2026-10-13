
import csv
import gzip
import io
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SNAPSHOT_FILE = Path("fo_contract_snapshot.json")
TELEGRAM_API = "https://api.telegram.org"

ARCHIVE_HOSTS = [
    "https://archives.nseindia.com/content/fo/",
    "https://nsearchives.nseindia.com/content/fo/",
]


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def download_contract_file():
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    # Try today's file and recent previous dates.
    for days_ago in range(8):
        date_to_try = today - timedelta(days=days_ago)
        filename = (
            f"NSE_FO_contract_{date_to_try.strftime('%d%m%Y')}.csv.gz"
        )

        for host in ARCHIVE_HOSTS:
            url = host + filename
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=40) as response:
                    compressed = response.read()

                # Reject HTML/error pages even if HTTP returned 200.
                content = gzip.decompress(compressed)
                text = content.decode("utf-8-sig", errors="replace")

                if not text.strip():
                    continue

                print("Downloaded contract file:", filename)
                return text, filename

            except Exception as error:
                print("Could not download", filename, "from", host)
                print(type(error).__name__)

    raise RuntimeError(
        "Could not download a recent NSE F&O contract file. "
        "No snapshot was changed."
    )


def find_column(headers, alternatives):
    lookup = {normalize(header): header for header in headers}

    for alternative in alternatives:
        if normalize(alternative) in lookup:
            return lookup[normalize(alternative)]

    return None


def parse_contracts(text):
    sample = text[:10000]

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",|;")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    if not reader.fieldnames:
        raise RuntimeError("NSE file has no readable column headers.")

    columns = {
        "instrument": find_column(
            reader.fieldnames,
            ["Instrument Type", "Instrument", "Instrument Name"],
        ),
        "symbol": find_column(
            reader.fieldnames,
            ["Symbol", "Underlying Symbol", "Underlying"],
        ),
        "expiry": find_column(
            reader.fieldnames,
            ["Expiry Date", "Expiry", "Expiration Date"],
        ),
        "strike": find_column(
            reader.fieldnames,
            ["Strike Price", "Strike"],
        ),
        "option_type": find_column(
            reader.fieldnames,
            ["Option Type", "Option Type Code", "Opt Type"],
        ),
        "lot_size": find_column(
            reader.fieldnames,
            ["Market Lot", "Lot Size", "Market Lot Size", "Market Lot Quantity"],
        ),
    }

    missing = [name for name, column in columns.items() if column is None]
    if missing:
        raise RuntimeError(
            "Could not identify these contract columns: "
            + ", ".join(missing)
            + ". Actual headers: "
            + ", ".join(reader.fieldnames)
        )

    contracts = {}

    for row in reader:
        instrument = str(row.get(columns["instrument"], "") or "").strip()
        symbol = str(row.get(columns["symbol"], "") or "").strip()
        expiry = str(row.get(columns["expiry"], "") or "").strip()
        strike = str(row.get(columns["strike"], "") or "").strip()
        option_type = str(row.get(columns["option_type"], "") or "").strip()
        lot_size = str(row.get(columns["lot_size"], "") or "").strip()

        if not instrument or not symbol or not expiry:
            continue

        # Identify a contract by its specifications, not its market price.
        identity = "|".join(
            normalize(part)
            for part in (instrument, symbol, expiry, strike, option_type)
        )

        if identity in contracts:
            raise RuntimeError(
                "Duplicate contract identity found. "
                "Stopping to avoid incorrect alerts."
            )

        contracts[identity] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "lot_size": lot_size,
        }

    if len(contracts) < 100:
        raise RuntimeError(
            f"Only {len(contracts)} contracts were parsed. "
            "This may be an incomplete file, so no snapshot was changed."
        )

    print("Contracts parsed:", len(contracts))
    return contracts


def compare_contracts(previous, current):
    old_keys = set(previous)
    new_keys = set(current)

    added = [current[key] for key in sorted(new_keys - old_keys)]
    removed = [previous[key] for key in sorted(old_keys - new_keys)]

    lot_changes = []
    for key in sorted(old_keys & new_keys):
        old_lot = previous[key]["lot_size"]
        new_lot = current[key]["lot_size"]

        if old_lot != new_lot:
            lot_changes.append({
                "contract": current[key],
                "old_lot": old_lot,
                "new_lot": new_lot,
            })

    return added, removed, lot_changes


def contract_label(contract):
    parts = [
        contract["symbol"],
        contract["instrument"],
        "Expiry " + contract["expiry"],
    ]

    if contract["strike"]:
        parts.append("Strike " + contract["strike"])

    if contract["option_type"]:
        parts.append(contract["option_type"])

    return " | ".join(parts)


def build_report(added, removed, lot_changes):
    lines = ["NSE F&O CONTRACT CHANGES", ""]

    lines.append(f"New contracts: {len(added)}")
    lines.append(f"Removed contracts: {len(removed)}")
    lines.append(f"Lot-size changes: {len(lot_changes)}")
    lines.append("")

    # Keep Telegram messages readable if many contracts change.
    limit = 25

    for item in lot_changes[:limit]:
        lines.append(
            f"LOT SIZE: {contract_label(item['contract'])}\n"
            f"Old: {item['old_lot']} -> New: {item['new_lot']}"
        )

    for contract in added[:limit]:
        lines.append("ADDED: " + contract_label(contract))

    for contract in removed[:limit]:
        lines.append("REMOVED: " + contract_label(contract))

    total = len(added) + len(removed) + len(lot_changes)
    if total > limit:
        lines.extend([
            "",
            f"Showing up to {limit} entries per category. "
            "Check the workflow log for totals.",
        ])

    return "\n\n".join(lines)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing.")

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    # Split long reports into Telegram-safe chunks.
    chunks = []
    while message:
        if len(message) <= 3500:
            chunks.append(message)
            break

        split_at = message.rfind("\n", 0, 3500)
        if split_at < 1:
            split_at = 3500

        chunks.append(message[:split_at])
        message = message[split_at:].lstrip()

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

        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            raise RuntimeError("Telegram did not accept the message.")

    print("Telegram notification sent.")


def main():
    print("Starting NSE F&O contract monitor...")

    text, filename = download_contract_file()
    current = parse_contracts(text)

    # First successful run establishes the baseline.
    if not SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.write_text(
            json.dumps(current, indent=2),
            encoding="utf-8",
        )

        send_telegram(
            "F&O CONTRACT MONITOR: BASELINE CREATED\n"
            f"File: {filename}\n"
            f"Contracts recorded: {len(current)}\n\n"
            "The next successful run will compare contracts "
            "against this snapshot."
        )
        return

    previous = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))

    added, removed, lot_changes = compare_contracts(previous, current)

    print("Added:", len(added))
    print("Removed:", len(removed))
    print("Lot-size changes:", len(lot_changes))

    if not added and not removed and not lot_changes:
        report = "NO UPDATE FOR FO CONTRACT"
    else:
        report = build_report(added, removed, lot_changes)

    # Save the snapshot; the workflow will commit it for tomorrow.
    SNAPSHOT_FILE.write_text(
        json.dumps(current, indent=2),
        encoding="utf-8",
    )

    send_telegram(report)


if __name__ == "__main__":
    import urllib.parse
    main()
