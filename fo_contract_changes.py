import csv
import gzip
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone, date
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
MONITORED_INSTRUMENTS = {"FUTSTK", "FUTIDX"}
# The three expiry months you want to check.
# Update this list when you want to monitor a different quarter.
EXPECTED_EXPIRY_MONTHS = [10, 11, 12]
MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}
# ============================================================
# GENERAL HELPERS
# ============================================================
def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())
def parse_expiry(value):
    """
    Parse Unix timestamps and conventional date strings.
    IMPORTANT:
    The source expiry field is StockNm, as specified.
    """
    value = str(value or "").strip()
    if not value:
        return ""
    # Unix timestamps, including milliseconds.
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        if number > 0:
            return datetime.fromtimestamp(
                number,
                timezone.utc,
            ).astimezone(IST).date().isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    # Also handle ISO timestamps containing a time zone.
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        pass
    raise RuntimeError(f"Unrecognized expiry date: {value!r}")
def file_date(filename):
    match = re.search(
        r"NSE_FO_contract_(\d{2})(\d{2})(\d{4})\.csv\.gz",
        filename,
    )
    if not match:
        return None
    day, month, year = map(int, match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None
def month_label(expiry):
    parsed = date.fromisoformat(expiry)
    return f"{MONTH_NAMES[parsed.month]} {parsed.year}"
# ============================================================
# DOWNLOAD NSE CONTRACT FILE
# ============================================================
def download_contract_file():
    """
    Try today's archive first, then the previous seven days.
    """
    today = datetime.now(IST).date()
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
                    headers=HEADERS,
                )
                with urllib.request.urlopen(
                    request,
                    timeout=40,
                ) as response:
                    compressed = response.read()
                content = gzip.decompress(compressed)
                text = content.decode("utf-8-sig", errors="replace")
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
        "The saved snapshot has not been changed."
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
    Required source fields:
        Column B: FinInstrmNm -> FUTSTK / FUTIDX
        Column I: MinLot      -> Lot size
        Column S: StockNm     -> Expiry date
    The underlying symbol is read from TckrSymb.
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
        raise RuntimeError("NSE contract file has no headers.")
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
            ["StockNm"],
        ),
        "lot_size": find_column(
            reader.fieldnames,
            ["MinLot"],
        ),
    }
    missing = [
        name for name, column in columns.items()
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
        expiry = parse_expiry(row.get(columns["expiry"], ""))
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
                "Snapshot not changed."
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
            "The file may be incomplete or incorrectly parsed."
        )
    stock_contracts = [
        item for item in contracts.values()
        if item["instrument"] == "FUTSTK"
    ]
    index_contracts = [
        item for item in contracts.values()
        if item["instrument"] == "FUTIDX"
    ]
    print("\n========== NSE CONTRACT SUMMARY ==========")
    print("Total contracts:", len(contracts))
    print("Stock Futures contracts:", len(stock_contracts))
    print(
        "Unique stock symbols:",
        len({item["symbol"] for item in stock_contracts}),
    )
    print("Index Futures contracts:", len(index_contracts))
    print(
        "Unique index symbols:",
        len({item["symbol"] for item in index_contracts}),
    )
    print("==========================================\n")
    return contracts
# ============================================================
# SUMMARIZE CONTRACTS BY UNDERLYING
# ============================================================
def contract_summary(contracts):
    summary = {}
    for contract in contracts.values():
        key = (
            contract["instrument"],
            contract["symbol"],
        )
        summary.setdefault(key, []).append({
            "expiry": contract["expiry"],
            "lot_size": contract["lot_size"],
        })
    for key in summary:
        summary[key].sort(key=lambda item: item["expiry"])
    return summary
def check_expected_expiries(contracts):
    """
    For each underlying, check which configured expiry months
    are represented in the file.
    Example:
        Expected: Oct, Nov, Dec
        Available: Oct, Dec
        Missing: Nov
    """
    summary = contract_summary(contracts)
    results = []
    for (instrument, symbol), items in sorted(summary.items()):
        expiries = [item["expiry"] for item in items]
        available_months = {
            date.fromisoformat(expiry).month
            for expiry in expiries
        }
        missing_months = [
            month for month in EXPECTED_EXPIRY_MONTHS
            if month not in available_months
        ]
        if missing_months:
            results.append({
                "instrument": instrument,
                "symbol": symbol,
                "available_expiries": expiries,
                "missing_months": missing_months,
            })
    return results
# ============================================================
# LOAD SNAPSHOT
# ============================================================
def load_snapshot():
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"Unable to read snapshot: {error}")
    if not isinstance(data, dict):
        raise RuntimeError("Snapshot has an invalid format.")
    if (
        data.get("version") in (2, 3, 4)
        and isinstance(data.get("contracts"), dict)
    ):
        raw_contracts = data["contracts"]
        metadata = {
            "source_file": data.get("source_file", "Unknown"),
            "version": data.get("version"),
        }
    else:
        raw_contracts = data
        metadata = {
            "source_file": "Unknown",
            "version": 1,
        }
    cleaned = {}
    for contract in raw_contracts.values():
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
        expiry = parse_expiry(contract.get("expiry", ""))
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
    print("Previous snapshot:", metadata["source_file"])
    print("Previous contract count:", len(cleaned))
    return {
        "contracts": cleaned,
        "metadata": metadata,
    }
# ============================================================
# COMPARE SNAPSHOTS
# ============================================================
def compare_contracts(previous, current):
    old_summary = contract_summary(previous)
    new_summary = contract_summary(current)
    old_keys = set(previous)
    new_keys = set(current)
    added_contracts = [
        current[key] for key in sorted(new_keys - old_keys)
    ]
    removed_contracts = [
        previous[key] for key in sorted(old_keys - new_keys)
    ]
    old_symbols = set(old_summary)
    new_symbols = set(new_summary)
    added_symbols = sorted(new_symbols - old_symbols)
    removed_symbols = sorted(old_symbols - new_symbols)
    expiry_count_changes = []
    for key in sorted(old_symbols & new_symbols):
        old_expiries = [
            item["expiry"] for item in old_summary[key]
        ]
        new_expiries = [
            item["expiry"] for item in new_summary[key]
        ]
        if len(old_expiries) != len(new_expiries):
            expiry_count_changes.append({
                "instrument": key[0],
                "symbol": key[1],
                "old_count": len(old_expiries),
                "new_count": len(new_expiries),
                "old_expiries": old_expiries,
                "new_expiries": new_expiries,
            })
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
# BUILD REPORT
# ============================================================
def build_report(changes, current_file, previous_file, contracts):
    today = datetime.now(IST).strftime("%d-%b-%Y")
    added_symbols = changes["added_symbols"]
    removed_symbols = changes["removed_symbols"]
    added_contracts = changes["added_contracts"]
    removed_contracts = changes["removed_contracts"]
    expiry_changes = changes["expiry_count_changes"]
    lot_changes = changes["lot_changes"]
    missing_expiry_report = check_expected_expiries(contracts)
    lines = [
        "NSE F&O CONTRACT CHANGES MONITOR",
        f"Report date: {today}",
        f"Previous file: {previous_file}",
        f"Current file: {current_file}",
        "",
        "EXPECTED EXPIRY CHECK",
        "Expected months: "
        + ", ".join(MONTH_NAMES[m] for m in EXPECTED_EXPIRY_MONTHS),
        f"Underlyings missing expected months: {len(missing_expiry_report)}",
        "",
        f"Underlying symbols added: {len(added_symbols)}",
        f"Underlying symbols missing: {len(removed_symbols)}",
        f"Expiry contracts added: {len(added_contracts)}",
        f"Expiry contracts removed: {len(removed_contracts)}",
        f"Expiry-count changes: {len(expiry_changes)}",
        f"Lot-size changes: {len(lot_changes)}",
        "",
    ]
    if missing_expiry_report:
        lines.append("MISSING EXPECTED EXPIRY MONTHS")
        for item in missing_expiry_report:
            missing = ", ".join(
                MONTH_NAMES[m] for m in item["missing_months"]
            )
            available = ", ".join(
                month_label(expiry)
                for expiry in item["available_expiries"]
            )
            lines.append(
                f"• {item['instrument']} | {item['symbol']}"
            )
            lines.append(f"  Missing: {missing}")
            lines.append(f"  Available expiries: {available or 'None'}")
        lines.append("")
    else:
        lines.append("All monitored underlyings have the expected months.")
        lines.append("")
    if added_symbols:
        lines.append("NEW UNDERLYING SYMBOLS")
        for instrument, symbol in added_symbols:
            lines.append(f"• {instrument} | {symbol}")
        lines.append("")
    if removed_symbols:
        lines.append("SYMBOLS MISSING FROM CURRENT FILE")
        for instrument, symbol in removed_symbols:
            lines.append(f"• {instrument} | {symbol}")
        lines.append("")
    if added_contracts:
        lines.append("NEW EXPIRY CONTRACTS")
        for item in added_contracts:
            lines.append(
                f"• {item['instrument']} | {item['symbol']} | "
                f"Expiry: {item['expiry']} | Lot: {item['lot_size']}"
            )
        lines.append("")
    if removed_contracts:
        lines.append("EXPIRY CONTRACTS NO LONGER PRESENT")
        for item in removed_contracts:
            lines.append(
                f"• {item['instrument']} | {item['symbol']} | "
                f"Expiry: {item['expiry']} | "
                f"Previous lot: {item['lot_size']}"
            )
        lines.append("")
    if expiry_changes:
        lines.append("EXPIRY-COUNT CHANGES")
        for item in expiry_changes:
            lines.append(
                f"• {item['instrument']} | {item['symbol']}: "
                f"{item['old_count']} → {item['new_count']} expiries"
            )
            lines.append(
                f"  Previous: {', '.join(item['old_expiries'])}"
            )
            lines.append(
                f"  Current: {', '.join(item['new_expiries'])}"
            )
        lines.append("")
    if lot_changes:
        lines.append("LOT-SIZE CHANGES")
        for item in lot_changes:
            lines.append(
                f"• {item['instrument']} | {item['symbol']} | "
                f"Expiry: {item['expiry']} | "
                f"{item['old_lot']} → {item['new_lot']}"
            )
        lines.append("")
    return "\n".join(lines).strip()
# ============================================================
# SAVE SNAPSHOT
# ============================================================
def save_snapshot(contracts, filename):
    payload = {
        "version": 4,
        "source_file": filename,
        "updated_at": datetime.now(IST).isoformat(),
        "contracts": contracts,
    }
    temporary = SNAPSHOT_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    temporary.replace(SNAPSHOT_FILE)
    print("Snapshot saved:", SNAPSHOT_FILE)
# ============================================================
# TELEGRAM WITH RETRIES
# ============================================================
def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "8430921727")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing.")
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
        for attempt in range(1, 5):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded"
                },
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=30,
                ) as response:
                    result = json.loads(
                        response.read().decode("utf-8")
                    )
                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram API error: {result}"
                    )
                print(f"Telegram message sent (attempt {attempt}).")
                break
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as error:
                if attempt == 4:
                    raise RuntimeError(
                        "Telegram failed after four attempts. "
                        "Snapshot not saved."
                    ) from error
                wait_seconds = attempt * 5
                print(
                    f"Telegram attempt {attempt} failed: {error}. "
                    f"Retrying in {wait_seconds} seconds..."
                )
                time.sleep(wait_seconds)
# ============================================================
# MAIN
# ============================================================
def main():
    print("Starting NSE F&O contract monitor...")
    text, filename = download_contract_file()
    current = parse_contracts(text)
    snapshot = load_snapshot()
    if snapshot is None:
        stock_symbols = {
            item["symbol"] for item in current.values()
            if item["instrument"] == "FUTSTK"
        }
        index_symbols = {
            item["symbol"] for item in current.values()
            if item["instrument"] == "FUTIDX"
        }
        missing = check_expected_expiries(current)
        message = (
            "NSE F&O CONTRACT CHANGES MONITOR\n"
            "Baseline created successfully.\n\n"
            f"Source file: {filename}\n"
            f"Unique stock symbols: {len(stock_symbols)}\n"
            f"Unique index symbols: {len(index_symbols)}\n"
            f"Total contracts: {len(current)}\n"
            f"Underlyings missing expected expiry months: {len(missing)}\n\n"
            "Future runs will compare expiry months and lot sizes."
        )
        send_telegram(message)
        save_snapshot(current, filename)
        return
    previous = snapshot["contracts"]
    previous_filename = snapshot["metadata"]["source_file"]
    old_date = file_date(previous_filename)
    new_date = file_date(filename)
    print("Previous source file:", previous_filename)
    print("Current source file:", filename)
    if previous_filename == filename:
        previous_has_indices = any(
            item["instrument"] == "FUTIDX"
            for item in previous.values()
        )
        current_has_indices = any(
            item["instrument"] == "FUTIDX"
            for item in current.values()
        )
        if current_has_indices and not previous_has_indices:
            message = (
                "NSE F&O CONTRACT CHANGES MONITOR\n"
                "Snapshot upgraded successfully.\n\n"
                f"Source file: {filename}\n"
                "FUTIDX has been added to the baseline.\n"
                "No additions or removals are claimed from this "
                "same-file comparison."
            )
            send_telegram(message)
            save_snapshot(current, filename)
            return
        # Same source file: still report its current expiry-month
        # check, but don't compare the file against itself.
        missing = check_expected_expiries(current)
        lines = [
            "NSE F&O CONTRACT CHANGES MONITOR",
            "No newer NSE contract file is available.",
            f"Source file: {filename}",
            "",
            "EXPECTED EXPIRY CHECK",
            "Expected months: "
            + ", ".join(MONTH_NAMES[m] for m in EXPECTED_EXPIRY_MONTHS),
            f"Underlyings missing expected months: {len(missing)}",
            "",
        ]
        if missing:
            for item in missing:
                missing_names = ", ".join(
                    MONTH_NAMES[m] for m in item["missing_months"]
                )
                available = ", ".join(
                    month_label(e) for e in item["available_expiries"]
                )
                lines.append(
                    f"• {item['instrument']} | {item['symbol']} "
                    f"| Missing: {missing_names} "
                    f"| Available: {available or 'None'}"
                )
        else:
            lines.append("All monitored underlyings have the expected months.")
        send_telegram("\n".join(lines))
        return
    if old_date and new_date and new_date < old_date:
        message = (
            "NSE F&O CONTRACT CHANGES MONITOR\n"
            "WARNING: The available NSE file is older than "
            "the saved snapshot.\n\n"
            f"Saved snapshot: {previous_filename}\n"
            f"Downloaded file: {filename}\n\n"
            "The snapshot has not been changed."
        )
        send_telegram(message)
        return
    changes = compare_contracts(previous, current)
    report = build_report(
        changes,
        filename,
        previous_filename,
        current,
    )
    # Only save the snapshot after Telegram delivery succeeds.
    send_telegram(report)
    save_snapshot(current, filename)
if __name__ == "__main__":
    main()
