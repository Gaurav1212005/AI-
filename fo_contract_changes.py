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

MINIMUM_STOCKS = 10


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def parse_expiry(value):
    """Parse NSE Unix timestamps or conventional date strings."""
    value = str(value or "").strip()

    if not value:
        return ""

    try:
        number = float(value)

        if number > 0:
            # NSE XpryDt is normally a Unix timestamp in seconds.
            return datetime.fromtimestamp(
                number, timezone.utc
            ).astimezone(IST).date().isoformat()

    except (ValueError, OverflowError, OSError):
        pass

    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue

    raise RuntimeError(f"Unrecognized expiry date: {value!r}")


def download_contract_file():
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

                with urllib.request.urlopen(request, timeout=40) as response:
                    compressed = response.read()

                content = gzip.decompress(compressed)
                text = content.decode("utf-8-sig", errors="replace")

                if not text.strip():
                    continue

                print("Downloaded:", filename)
                return text, filename

            except Exception as error:
                print(
                    f"Download failed: {filename} | "
                    f"{host} | {type(error).__name__}"
                )

    raise RuntimeError(
        "Unable to download a recent NSE contract file. "
        "The previous snapshot has not been changed."
    )


def find_column(headers, candidates):
    lookup = {normalize(header): header for header in headers}

    for candidate in candidates:
        column = lookup.get(normalize(candidate))

        if column:
            return column

    return None


def parse_contracts(text):
    try:
        dialect = csv.Sniffer().sniff(
            text[:10000],
            delimiters=",|;",
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

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
            ["XpryDt"],
        ),
        "lot_size": find_column(
            reader.fieldnames,
            ["MinLot"],
        ),
    }

    missing = [
        name for name, column in columns.items() if not column
    ]

    if missing:
        raise RuntimeError(
            "Missing required NSE columns: "
            + ", ".join(missing)
        )

    contracts = {}

    for row in reader:
        instrument = str(
            row.get(columns["instrument"], "") or ""
        ).strip().upper()

        # Only individual stock Futures. Exclude Options and index Futures.
        if instrument != "FUTSTK":
            continue

        symbol = str(
            row.get(columns["symbol"], "") or ""
        ).strip().upper()

        expiry = parse_expiry(row.get(columns["expiry"], ""))

        lot_size = str(
            row.get(columns["lot_size"], "") or ""
        ).strip()

        if not symbol or not expiry or not lot_size:
            continue

        try:
            if float(lot_size) <= 0:
                continue
        except ValueError:
            continue

        # A stock can have several monthly Futures contracts.
        # Keep each expiry for comparison, but aggregate by stock for
        # additions and removals.
        key = f"{symbol}|{expiry}"

        if key in contracts:
            raise RuntimeError(
                f"Duplicate stock Futures contract: {key}. "
                "Snapshot not changed."
            )

        contracts[key] = {
            "symbol": symbol,
            "instrument": "FUTSTK",
            "expiry": expiry,
            "lot_size": lot_size,
        }

    stocks = {contract["symbol"] for contract in contracts.values()}

    if len(stocks) < MINIMUM_STOCKS:
        raise RuntimeError(
            f"Only {len(stocks)} unique stock Futures found. "
            "This may be an incomplete or incorrectly parsed file."
        )

    print("Stock Futures contracts:", len(contracts))
    print("Unique stocks:", len(stocks))

    return contracts


def stock_summary(contracts):
    """Group monthly Futures contracts by underlying stock."""
    result = {}

    for contract in contracts.values():
        symbol = contract["symbol"]

        result.setdefault(symbol, []).append({
            "expiry": contract["expiry"],
            "lot_size": contract["lot_size"],
        })

    for symbol in result:
        result[symbol].sort(key=lambda item: item["expiry"])

    return result


def load_snapshot():
    if not SNAPSHOT_FILE.exists():
        return None

    data = json.loads(
        SNAPSHOT_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise RuntimeError("Snapshot has an invalid format.")

    # Support both the new format and the previous contract-level snapshot.
    if data.get("version") == 2 and isinstance(data.get("contracts"), dict):
        contracts = data["contracts"]
    else:
        contracts = data

    cleaned = {}

    for key, contract in contracts.items():
        if not isinstance(contract, dict):
            continue

        if str(contract.get("instrument", "")).upper() != "FUTSTK":
            continue

        symbol = str(contract.get("symbol", "")).strip().upper()

        if not symbol:
            continue

        expiry = parse_expiry(contract.get("expiry", ""))
        lot_size = str(contract.get("lot_size", "")).strip()

        if not expiry or not lot_size:
            continue

        cleaned[f"{symbol}|{expiry}"] = {
            "symbol": symbol,
            "instrument": "FUTSTK",
            "expiry": expiry,
            "lot_size": lot_size,
        }

    return cleaned


def compare_contracts(previous, current):
    old_stocks = stock_summary(previous)
    new_stocks = stock_summary(current)

    added_stocks = sorted(set(new_stocks) - set(old_stocks))
    missing_stocks = sorted(set(old_stocks) - set(new_stocks))

    # A missing stock is only a potential removal.
    # This file alone does not confirm official NSE exclusion.
    potential_removals = []

    for symbol in missing_stocks:
        potential_removals.append({
            "symbol": symbol,
            "previous_expiries": [
                item["expiry"] for item in old_stocks[symbol]
            ],
        })

    lot_changes = []

    for symbol in sorted(set(old_stocks) & set(new_stocks)):
        old_by_expiry = {
            item["expiry"]: item["lot_size"]
            for item in old_stocks[symbol]
        }

        new_by_expiry = {
            item["expiry"]: item["lot_size"]
            for item in new_stocks[symbol]
        }

        for expiry in sorted(set(old_by_expiry) & set(new_by_expiry)):
            old_lot = old_by_expiry[expiry]
            new_lot = new_by_expiry[expiry]

            if old_lot != new_lot:
                lot_changes.append({
                    "symbol": symbol,
                    "expiry": expiry,
                    "old_lot": old_lot,
                    "new_lot": new_lot,
                })

    return added_stocks, potential_removals, lot_changes


def build_report(added, removals, lot_changes, filename):
    today = datetime.now(IST).strftime("%d-%b-%Y")

    lines = [
        "NSE STOCK FUTURES MONITOR",
        f"Date: {today}",
        f"Contract file: {filename}",
        "",
        f"Stocks added: {len(added)}",
        f"Potential removals: {len(removals)}",
        f"Lot-size changes: {len(lot_changes)}",
        "",
    ]

    if added:
        lines.append("NEW STOCKS IN FUTURES")

        for symbol in added:
            lines.append(f"• {symbol}")

        lines.append("")

    if removals:
        lines.append("POTENTIAL STOCK REMOVALS")

        for item in removals:
            expiries = ", ".join(item["previous_expiries"])
            lines.append(f"• {item['symbol']} | Previously: {expiries}")

        lines.append(
            "These stocks were absent from the current file. "
            "Confirm against NSE's official exclusion tracker."
        )
        lines.append("")

    if lot_changes:
        lines.append("LOT-SIZE CHANGES")

        for item in lot_changes:
            lines.append(
                f"• {item['symbol']} | Expiry: {item['expiry']}\n"
                f"  Old lot: {item['old_lot']} → "
                f"New lot: {item['new_lot']}"
            )

    return "\n".join(lines).strip()


def save_snapshot(contracts, filename):
    payload = {
        "version": 2,
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
            raise RuntimeError(
                f"Telegram send failed: {result}"
            )

    print("Telegram notification sent.")


def main():
    print("Starting NSE Stock Futures monitor...")

    text, filename = download_contract_file()
    current = parse_contracts(text)

    previous = load_snapshot()

    # First run creates a baseline; it does not claim stocks were added.
    if previous is None:
        save_snapshot(current, filename)

        send_telegram(
            "NSE STOCK FUTURES MONITOR\n"
            "Baseline created successfully.\n"
            f"Source file: {filename}\n"
            f"Unique stocks: "
            f"{len(stock_summary(current))}\n\n"
            "Changes will be reported on the next successful run."
        )
        return

    added, removals, lot_changes = compare_contracts(
        previous,
        current,
    )

    print("New stocks:", len(added))
    print("Potential removals:", len(removals))
    print("Lot-size changes:", len(lot_changes))

    if not added and not removals and not lot_changes:
        report = "NO UPDATE FOR FO FUTURES CONTRACT"
    else:
        report = build_report(
            added,
            removals,
            lot_changes,
            filename,
        )

    # Save only after download and parsing succeeded.
    save_snapshot(current, filename)
    send_telegram(report)


if __name__ == "__main__":
    main()
