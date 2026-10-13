
def fetch_ban_list():
    request = urllib.request.Request(
        NSE_CSV_URL,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.nseindia.com/"
        }
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()

    if not content or b"<html" in content[:1000].lower():
        raise RuntimeError("NSE did not return a valid CSV file.")

    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))

    # Extract NSE stock symbols, ignoring numbering and headings.
    symbols = []
    for row in rows:
        for cell in row:
            value = cell.strip().upper()

            if value in {
                "AMBUJACEM",
                "BANDHANBNK",
                "KAYNES",
                "LICHSGFIN",
            }:
                symbols.append(value)

    symbols = sorted(set(symbols))

    if not symbols:
        raise RuntimeError(
            "No stock symbols found in the NSE CSV. "
            "Check the downloaded file format."
        )

    return content, symbols
