
import gzip
import urllib.request
from pathlib import Path

NSE_HOME = "https://www.nseindia.com/all-reports-derivatives"


def main():
    print("Connecting to NSE...")

    request = urllib.request.Request(
        NSE_HOME,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
        print("NSE page status:", response.status)

    if "F&O-MII - Contract File" in html or "Contract File (.gz)" in html:
        print("NSE page mentions the contract file.")
    else:
        print("The contract-file label was not found in the page HTML.")

    print("Initial connection test completed.")
    print("No existing monitor files were changed.")


if __name__ == "__main__":
    main()
