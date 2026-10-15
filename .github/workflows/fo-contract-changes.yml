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
# NSE CONTRACT FILE DOWNLOAD
# Start with yesterday's file and look back up to 10 days.
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

            # Verify that the response is a valid gzip file.
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
# Extract from StockNm, e.g. NIFTY26OCTFUT -> 2026-10
# ============================================================

def extract_expiry_month(stock_name):
    if not stock_name:
        return None

    stock_name = stock_name.strip().upper()

    pattern = (
        r"(\d{2})"
        r"(" + "|".join(MONTH_NAMES) + r")"
    )

    matches = list(re.finditer(pattern, stock_name))

    if not matches:
        return None

    match = matches[-1]

    year_suffix = int(match.group(1))
    month_name = match.group(2)

    year = 2000 + year_suffix
    month = MONTHS[month_name]

    return f"{year:04d}-{month:02d}"


# ============================================================
# CSV PROCESSING
# ============================================================

def read_contracts(csv_data):
    text = csv_data.decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise ValueError("The NSE contract CSV has no header row.")

    # Normalize column names without changing their values.
    column_map = {
        name.strip().lower(): name
        for name in reader.fieldnames
        if name
    }

    required_columns = [
        "stocknm",
        "tckrsymb",
        "fininstrmtp",
    ]

    missing_columns = [
        column
        for column in required_columns
        if column not in column_map
    ]

    if missing_columns:
        raise ValueError(
            "Unexpected NSE contract file format. "
            f"Missing columns: {', '.join(missing_columns)}. "
            f"Available columns: {', '.join(reader.fieldnames)}"
        )

    stock_column = column_map["stocknm"]
    symbol_column = column_map["tckrsymb"]
    type_column = column_map["fininstrmtp"]

    lot_column = column_map.get("minlot")

    contracts = {}

    for row in reader:
        instrument_type = (
            row.get(type_column) or ""
        ).strip().upper()

        if instrument_type not in EXPECTED_INSTRUMENT_TYPES:
            continue

        stock_name = (
            row.get(stock_column) or ""
        ).strip().upper()

        symbol = (
            row.get(symbol_column) or ""
        ).strip().upper()

        # Exclude test instruments from the entire report.
        if "NSETEST" in stock_name or "NSETEST" in symbol:
            continue

        if not symbol or not stock_name:
            continue

        expiry_month = extract_expiry_month(stock_name)

        if not expiry_month:
            continue

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

    if not contracts:
        raise ValueError(
            "No valid FUTSTK or FUTIDX contracts were found. "
            "The file may have an unexpected format."
        )

    return contracts


# ============================================================
# MISSING EXPIRY MONTHS
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
# SNAPSHOT
# Keep the snapshot updated for GitHub Actions.
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
# REPORT GENERATION
# Only show:
# 1. Report title
# 2. Generation time
# 3. Contract filename
# 4. Expected expiry months
# 5. Missing expiry months
#
# No contract counts, additions, removals, lot-size changes,
# NSETEST instruments, or explanatory footer.
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
        f"Report generated: {now.strftime('%d %b %Y, %I:%M %p')} IST",
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
# TELEGRAM DELIVERY
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

    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "NSE-FO-Contract-Monitor",
        },
        method="POST",
    )

    for attempt in range(1, 4):
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
            return

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

    # 1. Download the latest available contract file.
    contract_filename, csv_data = download_contract_file()

    # 2. Read FUTSTK and FUTIDX contracts.
    #    NSETEST instruments are excluded.
    contracts = read_contracts(csv_data)

    # 3. Determine expected expiry months.
    expected_months = get_expected_expiry_months()

    # 4. Identify missing expiry months.
    missing_months = find_missing_expiry_months(
        contracts,
        expected_months,
    )

    # 5. Generate the simplified report.
    report = generate_report(
        contract_filename,
        expected_months,
        missing_months,
    )

    print("\n" + report)

    # 6. Send the report to Telegram.
    send_telegram_message(report)

    # 7. Update the snapshot after successful delivery.
    save_snapshot(
        contracts,
        contract_filename,
        expected_months,
    )

    print("\nMonitor completed successfully.")


if __name__ == "__main__":
    main()
