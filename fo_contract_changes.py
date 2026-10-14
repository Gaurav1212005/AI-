import csv
import gzip
import io
import json
import os
import re
import urllib.request
import urllib.parse

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

SNAPSHOT_FILE = Path("fo_contract_snapshot.json")
TELEGRAM_API = "https://api.telegram.org"
IST = ZoneInfo("Asia/Kolkata")

ARCHIVE_HOSTS = [
    "https://archives.nseindia.com/content/fo/",
    "https://nsearchives.nseindia.com/content/fo/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

MINIMUM_CONTRACTS = 10

# Monitor stock futures and index futures.
MONITORED_INSTRUMENTS = {"FUTSTK", "FUTIDX"}


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def instrument_name(instrument):
    if instrument == "FUTSTK":
        return "STOCK FUTURES"

    if instrument == "FUTIDX":
        return "INDEX FUTURES"

    return instrument


def parse_expiry(value):
    """
    Parse NSE expiry dates.

    NSE files may contain Unix timestamps or conventional
    date strings.
    """
    value = str(value or "").strip()

    if not value:
        return ""

    try:
        number = float(value)

        if number > 0:
            return datetime.fromtimestamp(
                number,
                timezone.utc,
            ).astimezone(IST).date().isoformat()

    except (ValueError, OverflowError, OSError):
        pass

    formats = (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt,
            ).date().isoformat()

        except ValueError:
            continue

    raise RuntimeError(
        f"Unrecognized expiry date: {value!r}"
    )


# ============================================================
# DOWNLOAD NSE CONTRACT FILE
# ============================================================

def download_contract_file():
    """
    Download the most recent available NSE F&O contract file.

    The function tries today's date first and then works
    backwards for up to eight calendar days.
    """
    today = datetime.now(IST).date()

    for days_ago in range(8):
        date_to_try = today - timedelta(days=days_ago)

        filename = (
            "NSE_FO_contract_"
            f"{date_to_try.strftime('%d%m%Y')}.csv.gz"
        )

        for host in ARCHIVE_HOSTS:
            url = host + filename

            try:
                request = urllib.request.Request(
                    url,
                    headers=HEADERS,
                )

                with urllib.request.urlopen(
                    request,
                    timeout=40,
                ) as response:
                    compressed = response.read()

                content = gzip.decompress(compressed)

                text = content.decode(
                    "utf-8-sig",
                    errors="replace",
                )

                if not text.strip():
                    continue

                print("Downloaded file:", filename)
                print("Source URL:", url)

                return text, filename

            except Exception as error:
                print(
                    f"Download failed: {filename} | "
                    f"{host} | {type(error).__name__}: {error}"
                )

    raise RuntimeError(
        "Unable to download a recent NSE contract file. "
        "The existing snapshot has not been changed."
    )


# ============================================================
# PARSE NSE CONTRACT FILE
# ============================================================

def find_column(headers, candidates):
    lookup = {
        normalize(header): header
        for header in headers
    }

    for candidate in candidates:
        column = lookup.get(normalize(candidate))

        if column:
            return column

    return None


def parse_contracts(text):
    """
    Parse FUTSTK and FUTIDX records.

    Contract identity:
        INSTRUMENT|SYMBOL|EXPIRY

    Example:
        FUTSTK|IEX|2026-10-27
        FUTIDX|NIFTY|2026-10-27
    """
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
        raise RuntimeError(
            "The NSE contract file has no headers."
        )

    columns = {
        "instrument": find_column(
            reader.fieldnames,
            ["FinInstrmNm"],
        ),
        "symbol": find_column(
            reader.fieldnames,
            ["TckrSymb"],
        ),
        "expiry": find_column(
            reader.fieldnames,
            ["XpryDt"],
        ),
        "lot_size": find_column(
            reader.fieldnames,
            ["MinLot"],
        ),
    }

    missing = [
        name
        for name, column in columns.items()
        if not column
    ]

    if missing:
        raise RuntimeError(
            "Missing required NSE columns: "
            + ", ".join(missing)
            + ". Actual headers: "
            + ", ".join(reader.fieldnames)
        )

    contracts = {}

    for row in reader:
        instrument = str(
            row.get(columns["instrument"], "") or ""
        ).strip().upper()

        if instrument not in MONITORED_INSTRUMENTS:
            continue

        symbol = str(
            row.get(columns["symbol"], "") or ""
        ).strip().upper()

        if not symbol:
            continue

        expiry = parse_expiry(
            row.get(columns["expiry"], "")
        )

        lot_size = str(
            row.get(columns["lot_size"], "") or ""
        ).strip()

        if not expiry or not lot_size:
            continue

        try:
            if float(lot_size) <= 0:
                continue

        except ValueError:
            continue

        key = f"{instrument}|{symbol}|{expiry}"

        if key in contracts:
            raise RuntimeError(
                f"Duplicate contract detected: {key}. "
                "The snapshot will not be changed."
            )

        contracts[key] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "lot_size": lot_size,
        }

    if len(contracts) < MINIMUM_CONTRACTS:
        raise RuntimeError(
            f"Only {len(contracts)} contracts were parsed. "
            "The file may be incomplete or incorrectly parsed. "
            "The snapshot will not be changed."
        )

    stock_contracts = sum(
        1
        for contract in contracts.values()
        if contract["instrument"] == "FUTSTK"
    )

    index_contracts = sum(
        1
        for contract in contracts.values()
        if contract["instrument"] == "FUTIDX"
    )

    stock_symbols = {
        contract["symbol"]
        for contract in contracts.values()
        if contract["instrument"] == "FUTSTK"
    }

    index_symbols = {
        contract["symbol"]
        for contract in contracts.values()
        if contract["instrument"] == "FUTIDX"
    }

    print("\n========== NSE CONTRACT SUMMARY ==========")
    print("Total contracts:", len(contracts))
    print("Stock Futures contracts:", stock_contracts)
    print("Unique stock symbols:", len(stock_symbols))
    print("Index Futures contracts:", index_contracts)
    print("Unique index symbols:", len(index_symbols))
    print("==========================================\n")

    return contracts


# ============================================================
# SUMMARIZE EXPIRIES
# ============================================================

def contract_summary(contracts):
    """
    Group contracts by instrument and underlying symbol.

    Each symbol has a list of available expiry dates.
    """
    summary = {}

    for contract in contracts.values():
        instrument = contract["instrument"]
        symbol = contract["symbol"]

        summary.setdefault(
            (instrument, symbol),
            [],
        ).append({
            "expiry": contract["expiry"],
            "lot_size": contract["lot_size"],
        })

    for key in summary:
        summary[key].sort(
            key=lambda item: item["expiry"]
        )

    return summary


# ============================================================
# LOAD PREVIOUS SNAPSHOT
# ============================================================

def load_snapshot():
    if not SNAPSHOT_FILE.exists():
        return None

    try:
        data = json.loads(
            SNAPSHOT_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            f"Unable to read snapshot: {error}"
        )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Snapshot has an invalid format."
        )

    # Support both versioned snapshots and older snapshots.
    if (
        data.get("version") in (2, 3)
        and isinstance(data.get("contracts"), dict)
    ):
        contracts = data["contracts"]
        source_file = data.get("source_file", "Unknown")

    else:
        contracts = data
        source_file = "Previous snapshot (legacy format)"

    cleaned = {}

    for _, contract in contracts.items():
        if not isinstance(contract, dict):
            continue

        instrument = str(
            contract.get("instrument", "")
        ).strip().upper()

        if instrument not in MONITORED_INSTRUMENTS:
            continue

        symbol = str(
            contract.get("symbol", "")
        ).strip().upper()

        if not symbol:
            continue

        expiry = parse_expiry(
            contract.get("expiry", "")
        )

        lot_size = str(
            contract.get("lot_size", "")
        ).strip()

        if not expiry or not lot_size:
            continue

        key = f"{instrument}|{symbol}|{expiry}"

        cleaned[key] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "lot_size": lot_size,
        }

    print("Previous snapshot:", source_file)
    print("Previous contract count:", len(cleaned))

    return cleaned


# ============================================================
# COMPARE PREVIOUS AND CURRENT CONTRACTS
# ============================================================

def compare_contracts(previous, current):
    old_summary = contract_summary(previous)
    new_summary = contract_summary(current)

    old_keys = set(previous)
    new_keys = set(current)

    # Individual contracts that appeared or disappeared.
    added_contract_keys = sorted(new_keys - old_keys)
    removed_contract_keys = sorted(old_keys - new_keys)

    added_contracts = [
        current[key]
        for key in added_contract_keys
    ]

    removed_contracts = [
        previous[key]
        for key in removed_contract_keys
    ]

    # Compare underlying stock/index symbols.
    old_symbols = set(old_summary)
    new_symbols = set(new_summary)

    added_symbols = sorted(new_symbols - old_symbols)
    removed_symbols = sorted(old_symbols - new_symbols)

    # Compare number of available expiries for every symbol.
    expiry_count_changes = []

    for key in sorted(old_symbols & new_symbols):
        instrument, symbol = key

        old_count = len(old_summary[key])
        new_count = len(new_summary[key])

        if old_count != new_count:
            expiry_count_changes.append({
                "instrument": instrument,
                "symbol": symbol,
                "old_count": old_count,
                "new_count": new_count,
                "old_expiries": [
                    item["expiry"]
                    for item in old_summary[key]
                ],
                "new_expiries": [
                    item["expiry"]
                    for item in new_summary[key]
                ],
            })

    # Compare lot sizes for contracts with matching expiries.
    lot_changes = []

    for key in sorted(old_keys & new_keys):
        old_lot = str(previous[key]["lot_size"])
        new_lot = str(current[key]["lot_size"])

        if old_lot != new_lot:
            contract = current[key]

            lot_changes.append({
                "instrument": contract["instrument"],
                "symbol": contract["symbol"],
                "expiry": contract["expiry"],
                "old_lot": old_lot,
                "new_lot": new_lot,
            })

    return {
        "added_symbols": added_symbols,
        "removed_symbols": removed_symbols,
        "added_contracts": added_contracts,
        "removed_contracts": removed_contracts,
        "expiry_count_changes": expiry_count_changes,
        "lot_changes": lot_changes,
    }


# ============================================================
# BUILD TELEGRAM REPORT
# ============================================================

def build_report(changes, filename, previous_filename):
    today = datetime.now(IST).strftime("%d-%b-%Y")

    added_symbols = changes["added_symbols"]
    removed_symbols = changes["removed_symbols"]
    added_contracts = changes["added_contracts"]
    removed_contracts = changes["removed_contracts"]
    expiry_changes = changes["expiry_count_changes"]
    lot_changes = changes["lot_changes"]

    lines = [
        "NSE F&O CONTRACT CHANGES MONITOR",
        f"Report date: {today}",
        f"Previous file: {previous_filename}",
        f"Current file: {filename}",
        "",
        f"Underlying symbols added: {len(added_symbols)}",
        f"Underlying symbols missing: {len(removed_symbols)}",
        f"Expiry contracts added: {len(added_contracts)}",
        f"Expiry contracts removed: {len(removed_contracts)}",
        f"Expiry-count changes: {len(expiry_changes)}",
        f"Lot-size changes: {len(lot_changes)}",
        "",
    ]

    # --------------------------------------------------------
    # Underlying symbols added
    # --------------------------------------------------------

    if added_symbols:
        lines.append("NEW UNDERLYING SYMBOLS")

        for instrument, symbol in added_symbols:
            lines.append(
                f"• {instrument} | {symbol}"
            )

        lines.append("")

    # --------------------------------------------------------
    # Underlying symbols missing completely
    # --------------------------------------------------------

    if removed_symbols:
        lines.append("SYMBOLS MISSING FROM CURRENT FILE")

        for instrument, symbol in removed_symbols:
            previous_expiries = [
                item["expiry"]
                for item in contract_summary(
                    {
                        key: value
                        for key, value in {}
                    }
                ).get((instrument, symbol), [])
            ]

            # These symbols are no longer present in the current file.
            lines.append(
                f"• {instrument} | {symbol}"
            )

        lines.append(
            "Verify against NSE's official introduction/"
            "exclusion tracker before treating these as removals."
        )
        lines.append("")

    # --------------------------------------------------------
    # Individual contracts added
    # --------------------------------------------------------

    if added_contracts:
        lines.append("NEW EXPIRY CONTRACTS")

        for contract in added_contracts:
            lines.append(
                f"• {contract['instrument']} | "
                f"{contract['symbol']} | "
                f"Expiry: {contract['expiry']} | "
                f"Lot: {contract['lot_size']}"
            )

        lines.append("")

    # --------------------------------------------------------
    # Individual contracts removed
    # --------------------------------------------------------

    if removed_contracts:
        lines.append("EXPIRY CONTRACTS NO LONGER PRESENT")

        for contract in removed_contracts:
            lines.append(
                f"• {contract['instrument']} | "
                f"{contract['symbol']} | "
                f"Expiry: {contract['expiry']} | "
                f"Previous lot: {contract['lot_size']}"
            )

        lines.append(
            "A contract disappearing may be a normal expiry "
            "transition and does not by itself prove exclusion."
        )
        lines.append("")

    # --------------------------------------------------------
    # Expiry count changes
    # --------------------------------------------------------

    if expiry_changes:
        lines.append("EXPIRY-COUNT CHANGES")

        for item in expiry_changes:
            old_expiries = ", ".join(item["old_expiries"])
            new_expiries = ", ".join(item["new_expiries"])

            lines.append(
                f"• {item['instrument']} | {item['symbol']}: "
                f"{item['old_count']} → {item['new_count']} expiries"
            )

            lines.append(
                f"  Previous: {old_expiries}"
            )

            lines.append(
                f"  Current: {new_expiries}"
            )

        lines.append("")

    # --------------------------------------------------------
    # Lot-size changes
    # --------------------------------------------------------

    if lot_changes:
        lines.append("LOT-SIZE CHANGES")

        for item in lot_changes:
            lines.append(
                f"• {item['instrument']} | "
                f"{item['symbol']} | "
                f"Expiry: {item['expiry']}\n"
                f"  Lot: {item['old_lot']} → {item['new_lot']}"
            )

        lines.append("")

    if (
        not added_symbols
        and not removed_symbols
        and not added_contracts
        and not removed_contracts
        and not expiry_changes
        and not lot_changes
    ):
        lines.append(
            "NO UPDATE FOR F&O FUTURES CONTRACTS"
        )

    return "\n".join(lines).strip()


# ============================================================
# SAVE SNAPSHOT
# ============================================================

def save_snapshot(contracts, filename):
    payload = {
        "version": 3,
        "source_file": filename,
        "updated_at": datetime.now(IST).isoformat(),
        "contracts": contracts,
    }

    temporary = SNAPSHOT_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(SNAPSHOT_FILE)

    print("Snapshot saved:", SNAPSHOT_FILE)


# ============================================================
# SEND TELEGRAM MESSAGE
# ============================================================

def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID",
        "8430921727",
    )

    if not token or not chat_id:
        raise RuntimeError(
            "Telegram credentials are missing."
        )

    url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    while message:
        if len(message) <= 3500:
            chunk = message
            message = ""

        else:
            split_at = message.rfind("\n", 0, 3500)

            if split_at < 1:
                split_at = 3500

            chunk = message[:split_at]
            message = message[split_at:].lstrip()

        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
        }).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                )
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram send failed: {result}"
            )

    print("Telegram notification sent.")


# ============================================================
# MAIN
# ============================================================

def main():
    print("Starting NSE F&O contract monitor...")

    text, filename = download_contract_file()
    current = parse_contracts(text)

    previous = load_snapshot()

    # First run: create a baseline.
    if previous is None:
        save_snapshot(current, filename)

        stock_symbols = {
            contract["symbol"]
            for contract in current.values()
            if contract["instrument"] == "FUTSTK"
        }

        index_symbols = {
            contract["symbol"]
            for contract in current.values()
            if contract["instrument"] == "FUTIDX"
        }

        message = (
            "NSE F&O CONTRACT CHANGES MONITOR\n"
            "Baseline created successfully.\n\n"
            f"Source file: {filename}\n"
            f"Unique stock symbols: {len(stock_symbols)}\n"
            f"Unique index symbols: {len(index_symbols)}\n"
            f"Total contracts: {len(current)}\n\n"
            "The next successful run will compare "
            "expiry counts, contract additions/removals, "
            "and lot sizes."
        )

        send_telegram(message)
        return

    # Compare snapshots.
    changes = compare_contracts(
        previous,
        current,
    )

    old_metadata = {}

    try:
        saved_data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )

        if isinstance(saved_data, dict):
            old_metadata["source_file"] = saved_data.get(
                "source_file",
                "Unknown",
            )

    except (OSError, json.JSONDecodeError):
        old_metadata["source_file"] = "Unknown"

    previous_filename = old_metadata.get(
        "source_file",
        "Unknown",
    )

    print("\n========== CHANGE SUMMARY ==========")
    print(
        "Underlying symbols added:",
        len(changes["added_symbols"]),
    )
    print(
        "Underlying symbols missing:",
        len(changes["removed_symbols"]),
    )
    print(
        "Expiry contracts added:",
        len(changes["added_contracts"]),
    )
    print(
        "Expiry contracts removed:",
        len(changes["removed_contracts"]),
    )
    print(
        "Expiry-count changes:",
        len(changes["expiry_count_changes"]),
    )
    print(
        "Lot-size changes:",
        len(changes["lot_changes"]),
    )
    print("====================================\n")

    report = build_report(
        changes,
        filename,
        previous_filename,
    )

    # Update the snapshot after successful download and parsing.
    save_snapshot(current, filename)

    # Send report to Telegram.
    send_telegram(report)


if __name__ == "__main__":
    main()
