import csv
import gzip
import io
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ============================================================
# CONFIGURATION
# ============================================================
IST = ZoneInfo("Asia/Kolkata")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SNAPSHOT_FILE = "fo_contract_snapshot.json"

NSE_ARCHIVE_URL = (
    "https://archives.nseindia.com/content/fo/"
)

EXPECTED_INSTRUMENT_TYPES = {"FUTSTK", "FUTIDX"}

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

MONTH_NAMES = list(MONTHS.keys())

EXPIRY_PATTERN = re.compile(
    r"(\d{2})(" + "|".join(MONTH_NAMES) + r")FUT$"
)

# ============================================================
# EXPECTED EXPIRY MONTHS
# Current month + next two calendar months
# ============================================================
def get_expected_expiry_months():
    today = datetime.now(IST).date()
    expected = []

    year = today.year
    month = today.month

    for _ in range(3):
        expected.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1

    return expected

# ============================================================
# DOWNLOAD NSE CONTRACT FILE
# Try yesterday's file, then look back up to 10 days.
# ============================================================
def download_contract_file():
    today = datetime.now(IST).date()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com/",
    }

    for days_back in range(1, 11):
        file_date = today - timedelta(days=days_back)
        filename = (
            f"NSE_FO_contract_"
            f"{file_date.strftime('%d%m%Y')}.csv.gz"
        )
        url = NSE_ARCHIVE_URL + filename

        request = urllib.request.Request(
            url,
            headers=headers,
        )

        try:
            print(f"Checking NSE contract file: {filename}")

            with urllib.request.urlopen(
                request,
                timeout=30,
            ) as response:
                compressed_data = response.read()

            if not compressed_data:
                continue

            with gzip.GzipFile(
                fileobj=io.BytesIO(compressed_data)
            ) as gz:
                csv_data = gz.read()

            if not csv_data:
                continue

            print(f"Downloaded contract file: {filename}")
            return filename, csv_data

        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            EOFError,
        ) as error:
            print(f"Could not retrieve {filename}: {error}")
            continue

    raise RuntimeError(
        "Could not download a valid NSE F&O contract file "
        "from the previous 10 calendar days."
    )

# ============================================================
# EXPIRY MONTH EXTRACTION
# Extract expiry month from StockNm.
# Example: WIPRO26NOVFUT -> 2026-11
# ============================================================
def extract_expiry_month(stock_name):
    if not stock_name:
        return None

    stock_name = stock_name.strip().upper()

    match = EXPIRY_PATTERN.search(stock_name)
    if not match:
        return None

    year = 2000 + int(match.group(1))
    month = MONTHS[match.group(2)]

    return f"{year:04d}-{month:02d}"

# ============================================================
# CSV COLUMN DETECTION
# ============================================================
def normalize_column_name(name):
    return re.sub(
        r"[^a-z0-9]",
        "",
        (name or "").strip().lower(),
    )


def detect_csv_dialect(text):
    try:
        return csv.Sniffer().sniff(
            text[:10000],
            delimiters=",;\t|",
        )
    except csv.Error:
        return csv.excel


def read_contracts(csv_data):
    text = csv_data.decode(
        "utf-8-sig",
        errors="replace",
    )

    dialect = detect_csv_dialect(text)

    reader = csv.DictReader(
        io.StringIO(text),
        dialect=dialect,
    )

    if not reader.fieldnames:
        raise ValueError(
            "The NSE contract CSV has no header row."
        )

    column_map = {
        normalize_column_name(name): name
        for name in reader.fieldnames
        if name
    }

    print("Detected NSE CSV columns:")
    print(reader.fieldnames)

    def find_column(*possible_names):
        for name in possible_names:
            normalized = normalize_column_name(name)
            if normalized in column_map:
                return column_map[normalized]
        return None

    stock_column = find_column(
        "StockNm",
        "StockName",
        "Stock",
    )

    symbol_column = find_column(
        "TckrSymb",
        "TickerSymbol",
        "Symbol",
        "Underlying",
        "UnderlyingSymbol",
    )

    # NSE currently leaves FinInstrmTp blank. The instrument type
    # (FUTSTK / FUTIDX / OPTSTK / OPTIDX) is in FinInstrmNm.
    # Check both columns, per row, and use whichever has a value.
    type_columns = [
        column
        for column in (
            find_column("FinInstrmNm"),
            find_column(
                "FinInstrmTp",
                "FinInstrmType",
                "InstrumentType",
                "Instrument",
            ),
        )
        if column
    ]

    lot_column = find_column(
        "MinLot",
        "MarketLot",
        "LotSize",
    )

    if not stock_column or not type_columns:
        raise ValueError(
            "Unable to identify the required NSE CSV columns. "
            f"Detected columns: {reader.fieldnames}"
        )

    print(f"Instrument type columns used: {type_columns}")

    contracts = {}

    rows_read = 0
    matching_types = 0
    excluded_test = 0
    recognized_expiry_rows = 0

    for row in reader:
        rows_read += 1

        instrument_type = ""
        for column in type_columns:
            value = (row.get(column) or "").strip().upper()
            if value in EXPECTED_INSTRUMENT_TYPES:
                instrument_type = value
                break

        if not instrument_type:
            continue

        matching_types += 1

        stock_name = (
            row.get(stock_column) or ""
        ).strip().upper()

        symbol = (
            row.get(symbol_column) or ""
        ).strip().upper() if symbol_column else ""

        # Exclude all NSETEST instruments.
        if "NSETEST" in stock_name or "NSETEST" in symbol:
            excluded_test += 1
            continue

        if not stock_name:
            continue

        # Use StockNm as a fallback if the symbol column
        # is unavailable or the symbol value is blank.
        if not symbol:
            symbol = stock_name

        expiry_month = extract_expiry_month(stock_name)

        if not expiry_month:
            continue

        recognized_expiry_rows += 1

        lot_size = None
        if lot_column:
            lot_value = (
                row.get(lot_column) or ""
            ).strip()
            if lot_value:
                lot_size = lot_value

        key = (instrument_type, symbol)

        if key not in contracts:
            contracts[key] = {
                "instrument_type": instrument_type,
                "symbol": symbol,
                "expiry_months": set(),
                "lot_sizes": set(),
            }

        contracts[key]["expiry_months"].add(expiry_month)

        if lot_size is not None:
            contracts[key]["lot_sizes"].add(lot_size)

    print(f"CSV rows read: {rows_read}")
    print(f"FUTSTK/FUTIDX rows: {matching_types}")
    print(f"NSETEST rows excluded: {excluded_test}")
    print(f"Rows with recognized expiry months: {recognized_expiry_rows}")
    print(f"Unique contracts identified: {len(contracts)}")

    if not contracts:
        raise ValueError(
            "No valid FUTSTK or FUTIDX contracts were found. "
            "Check the detected CSV columns, instrument type values, "
            "and StockNm expiry format in the logs."
        )

    return contracts

# ============================================================
# FIND MISSING EXPIRY MONTHS
# ============================================================
def find_missing_expiry_months(
    contracts,
    expected_months,
):
    results = []
    expected_set = set(expected_months)

    for key in sorted(contracts):
        contract = contracts[key]
        present = contract["expiry_months"]
        missing = expected_set - present

        if not missing:
            continue

        results.append({
            "instrument_type": contract["instrument_type"],
            "symbol": contract["symbol"],
            "present": sorted(present),
            "missing": sorted(missing),
        })

    return results

# ============================================================
# SAVE SNAPSHOT
# ============================================================
def save_snapshot(
    contracts,
    contract_filename,
    expected_months,
):
    snapshot = {
        "generated_at_ist": datetime.now(IST).isoformat(),
        "contract_file": contract_filename,
        "expected_expiry_months": expected_months,
        "contracts": {},
    }

    for key in sorted(contracts):
        contract = contracts[key]

        snapshot_key = (
            f"{contract['instrument_type']}|"
            f"{contract['symbol']}"
        )

        snapshot["contracts"][snapshot_key] = {
            "instrument_type": contract["instrument_type"],
            "symbol": contract["symbol"],
            "expiry_months": sorted(
                contract["expiry_months"]
            ),
            "lot_sizes": sorted(
                contract["lot_sizes"]
            ),
        }

    with open(
        SNAPSHOT_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            snapshot,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Snapshot saved to {SNAPSHOT_FILE}")

# ============================================================
# GENERATE TELEGRAM REPORT
# Only:
# - Report title
# - Report time
# - Contract file
# - Expected expiry months
# - Missing expiry months
# ============================================================
def generate_report(
    contract_filename,
    expected_months,
    missing_months,
):
    now = datetime.now(IST)

    lines = [
        "NSE F&O CONTRACT CHANGE MONITOR",
        "",
        (
            "Report generated: "
            f"{now.strftime('%d %b %Y, %I:%M %p')} IST"
        ),
        f"Contract file: {contract_filename}",
        "",
        "EXPECTED EXPIRY MONTHS",
        ", ".join(expected_months),
        "",
        "MISSING EXPIRY MONTHS",
    ]

    if not missing_months:
        lines.append("None")
    else:
        for item in missing_months:
            present = ", ".join(item["present"])
            missing = ", ".join(item["missing"])

            lines.append(
                f"{item['symbol']} "
                f"({item['instrument_type']}): "
                f"present [{present}]; "
                f"missing [{missing}]"
            )

    return "\n".join(lines)

# ============================================================
# SEND TELEGRAM MESSAGE
# ============================================================
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is missing."
        )

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID environment variable is missing."
        )

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    # Telegram limit is 4096 characters per message.
    chunks = []
    current = ""
    for line in message.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk.rstrip("\n"),
            "disable_web_page_preview": True,
        }).encode("utf-8")

        for attempt in range(1, 4):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "NSE-FO-Contract-Monitor",
                },
                method="POST",
            )

            try:
                with urllib.request.urlopen(
                    request,
                    timeout=30,
                ) as response:
                    response_data = response.read().decode("utf-8")

                result = json.loads(response_data)

                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram API returned an error: {result}"
                    )

                print("Telegram message sent successfully.")
                break

            except Exception as error:
                print(
                    f"Telegram attempt {attempt}/3 failed: {error}"
                )

                if attempt == 3:
                    raise

                time.sleep(attempt * 3)

# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("NSE F&O CONTRACT CHANGE MONITOR")
    print(f"Current IST time: {datetime.now(IST)}")
    print("=" * 60)

    # Download the latest available contract file.
    contract_filename, csv_data = download_contract_file()

    # Parse contracts and exclude NSETEST instruments.
    contracts = read_contracts(csv_data)

    # Get current month and the next two calendar months.
    expected_months = get_expected_expiry_months()

    # Identify contracts missing expected expiry months.
    missing_months = find_missing_expiry_months(
        contracts,
        expected_months,
    )

    # Generate the report.
    report = generate_report(
        contract_filename,
        expected_months,
        missing_months,
    )

    print("\n" + report)

    # Deliver the report to Telegram.
    send_telegram_message(report)

    # Save snapshot after successful delivery.
    save_snapshot(
        contracts,
        contract_filename,
        expected_months,
    )

    print("\nMonitor completed successfully.")


if __name__ == "__main__":
    main()
