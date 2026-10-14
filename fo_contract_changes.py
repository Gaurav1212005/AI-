import csv
import gzip
import io
import json
import os
import re
import urllib.request
import urllib.parse
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


def expiry_month(value):
    """Convert an expiry date to YYYY-MM to ignore day-only changes."""
    value = str(value or "").strip()

    formats = [
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d%m%Y",
        "%d-%b-%Y %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m")
        except ValueError:
            continue

    # If the date format is unknown, retain the original normalized value.
    return normalize(value)


def download_contract_file():
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    for days_ago in range(8):
        date_to_try = today - timedelta(days=days_ago)

        filename = (
            f"NSE_FO_contract_{date_to_try.strftime('%d%m%Y')}.csv.gz"
        )

        for host in ARCHIVE_HOSTS:
            url = host + filename

            try:
                request = urllib.request.Request(
                    url,
                    headers=headers,
                )

                with urllib.request.urlopen(request, timeout=40) as response:
                    compressed = response.read()

                content = gzip.decompress(compressed)
                text = content.decode("utf-8-sig", errors="replace")

                if not text.strip():
                    continue

                print("Downloaded contract file:", filename)
                return text, filename

            except Exception as error:
                print(
                    "Could not download",
                    filename,
                    "from",
                    host,
                    type(error).__name__,
                )

    raise RuntimeError(
        "Could not download a recent NSE F&O contract file. "
        "No snapshot was changed."
    )


def find_column(headers, alternatives):
    lookup = {normalize(header): header for header in headers}

    for alternative in alternatives:
        key = normalize(alternative)

        if key in lookup:
            return lookup[key]

    return None


def parse_contracts(text):
    try:
        dialect = csv.Sniffer().sniff(
            text[:10000],
            delimiters=",|;",
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(
        io.StringIO(text),
        dialect=dialect,
    )

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
        "lot_size": find_column(
            reader.fieldnames,
            [
                "Market Lot",
                "Lot Size",
                "Market Lot Size",
                "Market Lot Quantity",
            ],
        ),
    }

    missing = [
        name
        for name, column in columns.items()
        if column is None
    ]

    if missing:
        raise RuntimeError(
            "Could not identify contract columns: "
            + ", ".join(missing)
            + ". Actual headers: "
            + ", ".join(reader.fieldnames)
        )

    contracts = {}

    for row in reader:
        instrument = str(
            row.get(columns["instrument"], "") or ""
        ).strip().upper()

        symbol = str(
            row.get(columns["symbol"], "") or ""
        ).strip()

        expiry = str(
            row.get(columns["expiry"], "") or ""
        ).strip()

        lot_size = str(
            row.get(columns["lot_size"], "") or ""
        ).strip()

        # Keep Futures only. Exclude all Options.
        if not instrument.startswith("FUT"):
            continue

        if not symbol or not expiry:
            continue

        # Identity excludes the expiry day and includes its month.
        # This prevents a day-only expiry change from triggering
        # a false addition or removal.
        identity = "|".join([
            normalize(instrument),
            normalize(symbol),
            expiry_month(expiry),
        ])

        if identity in contracts:
            raise RuntimeError(
                "Duplicate Futures contract identity found: "
                + identity
                + ". Stopping to avoid incorrect alerts."
            )

        contracts[identity] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "lot_size": lot_size,
        }

    if len(contracts) < 10:
        raise RuntimeError(
            f"Only {len(contracts)} Futures contracts were parsed. "
            "The file may be incomplete. Snapshot was not changed."
        )

    print("Futures contracts parsed:", len(contracts))
    return contracts


def load_previous_snapshot():
    if not SNAPSHOT_FILE.exists():
        return None

    raw = json.loads(
        SNAPSHOT_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(raw, dict):
        raise RuntimeError("Previous snapshot has an invalid format.")

    previous = {}

    for contract in raw.values():
        if not isinstance(contract, dict):
            continue

        instrument = str(
            contract.get("instrument", "")
        ).strip().upper()

        # Remove Options from the old snapshot.
        if not instrument.startswith("FUT"):
            continue

        symbol = str(contract.get("symbol", "")).strip()
        expiry = str(contract.get("expiry", "")).strip()

        if not symbol or not expiry:
            continue

        identity = "|".join([
            normalize(instrument),
            normalize(symbol),
            expiry_month(expiry),
        ])

        if identity in previous:
            raise RuntimeError(
                "Duplicate Futures identity in previous snapshot: "
                + identity
                + ". Snapshot was not changed."
            )

        previous[identity] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "lot_size": str(contract.get("lot_size", "")).strip(),
        }

    return previous


def compare_contracts(previous, current):
    old_keys = set(previous)
    new_keys = set(current)

    added = [
        current[key]
        for key in sorted(new_keys - old_keys)
    ]

    removed = [
        previous[key]
        for key in sorted(old_keys - new_keys)
    ]

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
    return (
        f"{contract['symbol']} FUTURES"
        f" | Expiry: {contract['expiry']}"
    )


def build_report(added, removed, lot_changes):
    today = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%d-%b-%Y")

    lines = [
        "NSE FUTURES CONTRACT CHANGES",
        f"Date: {today}",
        "",
        f"New Futures contracts: {len(added)}",
        f"Removed Futures contracts: {len(removed)}",
        f"Lot-size changes: {len(lot_changes)}",
        "",
    ]

    limit = 25

    if lot_changes:
        lines.append("LOT-SIZE CHANGES")

        for item in lot_changes[:limit]:
            lines.append(
                f"{item['contract']['symbol']} FUTURES\n"
                f"Old lot size: {item['old_lot']}\n"
                f"New lot size: {item['new_lot']}"
            )

        lines.append("")

    if added:
        lines.append("NEW FUTURES CONTRACTS")

        for contract in added[:limit]:
            lines.append(contract_label(contract))

        lines.append("")

    if removed:
        lines.append("REMOVED FUTURES CONTRACTS")

        for contract in removed[:limit]:
            lines.append(contract_label(contract))

    total = len(added) + len(removed) + len(lot_changes)

    if total > limit:
        lines.extend([
            "",
            "Some entries are omitted from this message.",
            f"Total changes detected: {total}",
        ])

    return "\n".join(lines).strip()


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing.")

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

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
            result = json.loads(
                response.read().decode("utf-8")
            )

        if not result.get("ok"):
            raise RuntimeError(
                "Telegram did not accept the message."
            )

    print("Telegram notification sent.")


def main():
    print("Starting NSE Futures contract monitor...")

    text, filename = download_contract_file()
    current = parse_contracts(text)

    previous = load_previous_snapshot()

    # The first successful run establishes the baseline.
    if previous is None:
        SNAPSHOT_FILE.write_text(
            json.dumps(current, indent=2),
            encoding="utf-8",
        )

        send_telegram(
            "NSE FUTURES MONITOR: BASELINE CREATED\n"
            f"File: {filename}\n"
            f"Futures contracts recorded: {len(current)}\n\n"
            "The next successful run will compare Futures "
            "contracts against this baseline."
        )
        return

    added, removed, lot_changes = compare_contracts(
        previous,
        current,
    )

    print("New Futures contracts:", len(added))
    print("Removed Futures contracts:", len(removed))
    print("Lot-size changes:", len(lot_changes))

    # Save only after the file has been downloaded and validated.
    SNAPSHOT_FILE.write_text(
        json.dumps(current, indent=2),
        encoding="utf-8",
    )

    if not added and not removed and not lot_changes:
        report = "NO UPDATE FOR FO FUTURES CONTRACT"
    else:
        report = build_report(
            added,
            removed,
            lot_changes,
        )

    send_telegram(report)


if __name__ == "__main__":
    main()
