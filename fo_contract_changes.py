import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
# ============================================================
# CONFIGURATION
# ============================================================
IST = ZoneInfo("Asia/Kolkata")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8430921727").strip()
SNAPSHOT_FILE = "fo_contract_snapshot.json"
SNAPSHOT_VERSION = 5
NSE_BASE_URLS = [
    "https://archives.nseindia.com/content/fo/",
    "https://nsearchives.nseindia.com/content/fo/",
]
INSTRUMENT_TYPES = {"FUTSTK", "FUTIDX"}
MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
MONTH_NAMES = {value: key for key, value in MONTHS.items()}
# Set to True if you want to report a missing expected expiry month
# for every underlying, even when that expiry is absent from the file.
CHECK_EXPECTED_EXPIRIES = True
# The expected months are calculated from the current IST month.
# For October 2026, the expected months are:
# 2026-10, 2026-11, 2026-12.
EXPECTED_EXPIRY_COUNT = 3
# Keep Telegram messages within a safe length.
TELEGRAM_MESSAGE_LIMIT = 3800
# ============================================================
# TIME AND LOGGING
# ============================================================
def log(message):
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{timestamp}] {message}", flush=True)
def expected_expiries():
    """Return the current month and the next two calendar months."""
    now = datetime.now(IST)
    year = now.year
    month = now.month
    result = []
    for _ in range(EXPECTED_EXPIRY_COUNT):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return result
# ============================================================
# NSE FILE DOWNLOAD
# ============================================================
def download_nse_contract_file():
    """
    Download today's NSE F&O contract file.
    The archive filename format is NSE_FO_contract_DDMMYYYY.csv.gz.
    """
    today = datetime.now(IST).strftime("%d%m%Y")
    filename = f"NSE_FO_contract_{today}.csv.gz"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/",
    }
    errors = []
    for base_url in NSE_BASE_URLS:
        url = base_url + filename
        request = urllib.request.Request(url, headers=headers)
        try:
            log(f"Trying NSE contract file: {url}")
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
            if not content:
                raise ValueError("Downloaded file is empty.")
            # Validate that the response is a gzip file.
            try:
                csv_bytes = gzip.decompress(content)
            except OSError:
                # Some archives may return an uncompressed CSV.
                csv_bytes = content
            if not csv_bytes.strip():
                raise ValueError("The CSV file is empty.")
            log(f"Downloaded contract file successfully: {filename}")
            return csv_bytes, filename
        except Exception as exc:
            error = f"{url}: {exc}"
            errors.append(error)
            log(f"Download attempt failed: {error}")
    raise RuntimeError(
        "Could not download the NSE F&O contract file.\n"
        + "\n".join(errors)
    )
# ============================================================
# CSV PARSING
# ============================================================
def decode_csv(csv_bytes):
    """Decode the downloaded CSV, handling common encodings."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return csv_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode the NSE CSV file.")
def normalize_text(value):
    if value is None:
        return ""
    return str(value).strip()
def parse_expiry_from_stock_name(stock_name):
    """
    Extract the expiry year and month from StockNm.
    Examples:
        NIFTY26OCTFUT       -> 2026-10
        360ONE26NOVFUT      -> 2026-11
        BANKNIFTY26DECFUT   -> 2026-12
    XpryDt is deliberately not used for expiry-month detection because
    the uploaded sample contains timestamps inconsistent with StockNm.
    """
    stock_name = normalize_text(stock_name).upper()
    pattern = (
        r"(\d{2})"
        r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
        r"FUT$"
    )
    match = re.search(pattern, stock_name)
    if not match:
        return None
    year_suffix = int(match.group(1))
    month_name = match.group(2)
    # NSE contract labels use two-digit years, e.g. 26 -> 2026.
    year = 2000 + year_suffix
    month = MONTHS[month_name]
    return f"{year:04d}-{month:02d}"
def read_contracts(csv_bytes):
    """
    Return a normalized contract snapshot.
    Snapshot structure:
        {
            "FUTSTK|RELIANCE|2026-10": {
                "instrument": "FUTSTK",
                "symbol": "RELIANCE",
                "expiry": "2026-10",
                "stock_name": "RELIANCE26OCTFUT",
                "lot_size": 250
            }
        }
    """
    text = decode_csv(csv_bytes)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV headers are missing.")
    reader.fieldnames = [
        normalize_text(name).lstrip("\ufeff")
        for name in reader.fieldnames
    ]
    required_columns = {
        "FinInstrmNm",
        "TckrSymb",
        "StockNm",
        "MinLot",
    }
    missing_columns = required_columns - set(reader.fieldnames)
    if missing_columns:
        raise ValueError(
            "The NSE CSV is missing required columns: "
            + ", ".join(sorted(missing_columns))
            + ". Available columns: "
            + ", ".join(reader.fieldnames)
        )
    contracts = {}
    rows_seen = 0
    skipped_rows = 0
    for row in reader:
        instrument = normalize_text(row.get("FinInstrmNm")).upper()
        if instrument not in INSTRUMENT_TYPES:
            continue
        rows_seen += 1
        symbol = normalize_text(row.get("TckrSymb")).upper()
        stock_name = normalize_text(row.get("StockNm")).upper()
        lot_size_text = normalize_text(row.get("MinLot"))
        if not symbol or not stock_name:
            skipped_rows += 1
            continue
        expiry = parse_expiry_from_stock_name(stock_name)
        if not expiry:
            skipped_rows += 1
            log(
                "Skipping contract because expiry could not be parsed "
                f"from StockNm: {stock_name}"
            )
            continue
        try:
            lot_size = int(float(lot_size_text))
        except (ValueError, TypeError):
            skipped_rows += 1
            log(
                f"Skipping {symbol} {stock_name}: "
                f"invalid MinLot value '{lot_size_text}'"
            )
            continue
        key = f"{instrument}|{symbol}|{expiry}"
        contracts[key] = {
            "instrument": instrument,
            "symbol": symbol,
            "expiry": expiry,
            "stock_name": stock_name,
            "lot_size": lot_size,
        }
    if not contracts:
        raise ValueError(
            "No valid FUTSTK/FUTIDX contracts were parsed. "
            "The snapshot will not be changed."
        )
    log(f"FUTSTK/FUTIDX rows inspected: {rows_seen}")
    log(f"Valid contracts parsed: {len(contracts)}")
    log(f"Rows skipped: {skipped_rows}")
    return contracts
# ============================================================
# SNAPSHOT MANAGEMENT
# ============================================================
def load_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            log("Existing snapshot is not a valid JSON object.")
            return None
        return data
    except Exception as exc:
        log(f"Could not read existing snapshot: {exc}")
        raise RuntimeError(
            "The existing snapshot could not be read. "
            "It has not been overwritten."
        )
def save_snapshot(contracts, source_file):
    """
    Save only after the Telegram message has been delivered successfully.
    """
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "updated_at_ist": datetime.now(IST).isoformat(),
        "source_file": source_file,
        "expected_expiries": expected_expiries(),
        "contracts": contracts,
    }
    temporary_file = SNAPSHOT_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(snapshot, file, indent=2, sort_keys=True)
    os.replace(temporary_file, SNAPSHOT_FILE)
    log("Snapshot saved successfully.")
# ============================================================
# CHANGE DETECTION
# ============================================================
def format_contract(contract):
    return (
        f"{contract['symbol']} "
        f"({contract['instrument']}, "
        f"{contract['expiry']}, "
        f"lot {contract['lot_size']})"
    )
def compare_contracts(old_contracts, new_contracts):
    old_keys = set(old_contracts)
    new_keys = set(new_contracts)
    added = [
        new_contracts[key]
        for key in sorted(new_keys - old_keys)
    ]
    removed = [
        old_contracts[key]
        for key in sorted(old_keys - new_keys)
    ]
    lot_changes = []
    for key in sorted(old_keys & new_keys):
        old = old_contracts[key]
        new = new_contracts[key]
        if old["lot_size"] != new["lot_size"]:
            lot_changes.append(
                {
                    "symbol": new["symbol"],
                    "instrument": new["instrument"],
                    "expiry": new["expiry"],
                    "old_lot": old["lot_size"],
                    "new_lot": new["lot_size"],
                }
            )
    return added, removed, lot_changes
def build_expiry_coverage_report(contracts):
    """
    Check whether each underlying has all expected expiry months.
    If an underlying has only one or two expected expiry months,
    report the missing month(s).
    """
    expected = expected_expiries()
    coverage = {}
    for contract in contracts.values():
        instrument = contract["instrument"]
        symbol = contract["symbol"]
        expiry = contract["expiry"]
        key = f"{instrument}|{symbol}"
        coverage.setdefault(key, set()).add(expiry)
    missing_items = []
    for key in sorted(coverage):
        instrument, symbol = key.split("|", 1)
        present = coverage[key]
        missing = [
            expiry
            for expiry in expected
            if expiry not in present
        ]
        if missing:
            missing_items.append(
                {
                    "instrument": instrument,
                    "symbol": symbol,
                    "present": sorted(present),
                    "missing": missing,
                }
            )
    return expected, missing_items
# ============================================================
# TELEGRAM
# ============================================================
def telegram_send_once(message):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN environment variable."
        )
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "Missing TELEGRAM_CHAT_ID environment variable."
        )
    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(
            "Telegram API returned an unsuccessful response: "
            + str(result)
        )
    return True
def telegram_send(message, attempts=3):
    """
    Retry Telegram delivery.
    A failed delivery raises an error, so the snapshot is not advanced.
    """
    for attempt in range(1, attempts + 1):
        try:
            telegram_send_once(message)
            log(f"Telegram message delivered (attempt {attempt}).")
            return
        except Exception as exc:
            log(
                f"Telegram delivery attempt {attempt}/{attempts} failed: "
                f"{exc}"
            )
            if attempt < attempts:
                time.sleep(3 * attempt)
    raise RuntimeError(
        "Telegram delivery failed after all retries. "
        "Snapshot has not been updated."
    )
def split_telegram_message(message):
    """
    Split a long message into multiple Telegram messages.
    Each part is sent separately.
    """
    if len(message) <= TELEGRAM_MESSAGE_LIMIT:
        return [message]
    lines = message.splitlines()
    chunks = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
def telegram_send_all(message):
    for part_number, part in enumerate(
        split_telegram_message(message), start=1
    ):
        if len(split_telegram_message(message)) > 1:
            part = f"Part {part_number}\n\n{part}"
        telegram_send(part)
# ============================================================
# REPORT CREATION
# ============================================================
def build_report(
    source_file,
    contracts,
    added,
    removed,
    lot_changes,
    expected,
    missing_items,
):
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    counts = {
        "FUTSTK": sum(
            1 for item in contracts.values()
            if item["instrument"] == "FUTSTK"
        ),
        "FUTIDX": sum(
            1 for item in contracts.values()
            if item["instrument"] == "FUTIDX"
        ),
    }
    lines = [
        "NSE F&O CONTRACT CHANGE MONITOR",
        f"Date/time: {now}",
        f"Contract file: {source_file}",
        "",
        "CURRENT CONTRACT COUNTS",
        f"FUTSTK: {counts['FUTSTK']}",
        f"FUTIDX: {counts['FUTIDX']}",
        f"Total: {len(contracts)}",
        "",
        "EXPECTED EXPIRY MONTHS",
        ", ".join(expected),
        "",
        "CONTRACT ADDITIONS",
    ]
    if added:
        lines.extend(f"+ {format_contract(item)}" for item in added)
    else:
        lines.append("None")
    lines.extend(["", "CONTRACT REMOVALS"])
    if removed:
        lines.extend(f"- {format_contract(item)}" for item in removed)
    else:
        lines.append("None")
    lines.extend(["", "LOT-SIZE CHANGES"])
    if lot_changes:
        for item in lot_changes:
            lines.append(
                f"{item['symbol']} ({item['instrument']}, "
                f"{item['expiry']}): "
                f"{item['old_lot']} -> {item['new_lot']}"
            )
    else:
        lines.append("None")
    lines.extend(["", "MISSING EXPIRY MONTHS"])
    if missing_items:
        for item in missing_items:
            lines.append(
                f"{item['symbol']} ({item['instrument']}): "
                f"present [{', '.join(item['present'])}]; "
                f"missing [{', '.join(item['missing'])}]"
            )
    else:
        lines.append("No missing expected expiry months detected.")
    lines.extend(
        [
            "",
            "Note: Expiry months are extracted from StockNm.",
            "The XpryDt field is not used for expiry-month detection.",
        ]
    )
    return "\n".join(lines)
# ============================================================
# MAIN
# ============================================================
def main():
    log("Starting NSE F&O contract monitor.")
    csv_bytes, source_file = download_nse_contract_file()
    current_contracts = read_contracts(csv_bytes)
    old_snapshot = load_snapshot()
    # Old snapshots used a different expiry interpretation.
    # Establish a fresh baseline instead of reporting false changes.
    if (
        old_snapshot is None
        or old_snapshot.get("version") != SNAPSHOT_VERSION
        or not isinstance(old_snapshot.get("contracts"), dict)
    ):
        expected, missing_items = build_expiry_coverage_report(
            current_contracts
        )
        lines = [
            "NSE F&O CONTRACT CHANGE MONITOR",
            f"Date/time: {datetime.now(IST).strftime('%d %b %Y, %I:%M %p IST')}",
            f"Contract file: {source_file}",
            "",
            "BASELINE CREATED",
            "The existing snapshot is missing or uses an older format.",
            "This run establishes a new baseline; additions and removals",
            "will be compared from the next successful run.",
            "",
            f"FUTSTK contracts: {sum(1 for c in current_contracts.values() if c['instrument'] == 'FUTSTK')}",
            f"FUTIDX contracts: {sum(1 for c in current_contracts.values() if c['instrument'] == 'FUTIDX')}",
            f"Total contracts: {len(current_contracts)}",
            "",
            "EXPECTED EXPIRY MONTHS",
            ", ".join(expected),
            "",
            "MISSING EXPIRY MONTHS",
        ]
        if missing_items:
            for item in missing_items:
                lines.append(
                    f"{item['symbol']} ({item['instrument']}): "
                    f"missing {', '.join(item['missing'])}"
                )
        else:
            lines.append("None")
        message = "\n".join(lines)
        telegram_send_all(message)
        save_snapshot(current_contracts, source_file)
        log("Baseline established successfully.")
        return
    old_contracts = old_snapshot["contracts"]
    added, removed, lot_changes = compare_contracts(
        old_contracts,
        current_contracts,
    )
    expected, missing_items = build_expiry_coverage_report(
        current_contracts
    )
    report = build_report(
        source_file,
        current_contracts,
        added,
        removed,
        lot_changes,
        expected,
        missing_items,
    )
    # Send the complete report before updating the snapshot.
    # If Telegram fails, execution stops and the old snapshot remains.
    telegram_send_all(report)
    save_snapshot(current_contracts, source_file)
    log(
        "Completed successfully. "
        f"Added={len(added)}, "
        f"Removed={len(removed)}, "
        f"Lot changes={len(lot_changes)}, "
        f"Missing-expiry alerts={len(missing_items)}."
    )
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
