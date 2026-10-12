"""
etf_sectors.py

Fetches ETF sector weightings via yfinance and writes them to an
'ETF Sectors' sheet in your portfolio Google Sheet.

No API key required — yfinance is free and covers both Canadian (.TO)
and US ETFs.

Usage:
    # Fetch all ETF tickers found in the Portfolio sheet (uses ETF column to filter)
    .venv/bin/python etf_sectors.py

    # Fetch specific tickers only
    .venv/bin/python etf_sectors.py QQQ XEQT ZCN VFV

Output — 'ETF Sectors' sheet (created/replaced on each run):
    Symbol | GF Symbol | Import Date | Communication Services | Consumer Discretionary | ...

    GF Symbol contains the Google Finance exchange:ticker format (e.g. TSX:XEQT, NASDAQ:QQQ)
    so it can be matched against the Portfolio table or used in GOOGLEFINANCE() formulas.

Notes:
    - Only rows with a truthy value in the 'ETF' column are fetched automatically.
    - TSX tickers are looked up as TICKER.TO (e.g. XEQT → XEQT.TO).
    - TSX Venture tickers are looked up as TICKER.V.
    - US tickers are looked up as-is (no suffix).
    - Sector weights are expressed as decimals (e.g. 0.2531 = 25.31%).
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


# ── Configuration ──────────────────────────────────────────────────────────────

SHEET_NAME_PORTFOLIO  = "Portfolio"
SHEET_NAME_ETF        = "ETF Sectors"
CONFIG_DIR            = Path.home() / ".config" / "portfolio_pipeline"
GOOGLE_CREDS          = CONFIG_DIR / "google_credentials.json"
SHEETS_CONFIG         = CONFIG_DIR / "sheets_config.json"
SHEETS_API_SCOPE      = ["https://www.googleapis.com/auth/spreadsheets"]
REQUEST_INTERVAL_S    = 1   # yfinance is free; short pause to be polite

# Column names in the Portfolio table
PORTFOLIO_COL_TICKER   = "Ticker"
PORTFOLIO_COL_EXCHANGE = "Exchange"
PORTFOLIO_COL_ETF      = "ETF"

# Maps Exchange values to Yahoo Finance ticker suffixes.
# Accepts both legacy aliases (TSX, VENTURE) and canonical GF codes (TSE, CVE).
EXCHANGE_TO_YF_SUFFIX: dict = {
    "TSE":      ".TO",
    "TSX":      ".TO",   # legacy alias
    "CVE":      ".V",
    "VENTURE":  ".V",    # legacy alias
    "NASDAQ":   "",
    "NYSE":     "",
    "NYSEARCA": "",
    "BATS":     "",
}

# Maps Exchange values to Google Finance exchange prefixes (for EXCHANGE:TICKER format).
# Canonical GF codes are TSE and CVE for Canadian exchanges.
EXCHANGE_TO_GF_PREFIX: dict = {
    "TSE":      "TSE",
    "TSX":      "TSE",   # legacy alias
    "CVE":      "CVE",
    "VENTURE":  "CVE",   # legacy alias
    "NASDAQ":   "NASDAQ",
    "NYSE":     "NYSE",
    "NYSEARCA": "NYSEARCA",
    "BATS":     "BATS",
}


# ── yfinance helpers ───────────────────────────────────────────────────────────

def to_gf_symbol(ticker: str, exchange: Optional[str]) -> str:
    """
    Convert a bare ticker + exchange name into Google Finance format.

        TSX  + XEQT  →  TSX:XEQT
        VENTURE + GPV  →  TSXV:GPV
        NASDAQ + QQQ  →  NASDAQ:QQQ
        unknown       →  ticker as-is
    """
    if not exchange:
        return ticker
    prefix = EXCHANGE_TO_GF_PREFIX.get(exchange.upper())
    return f"{prefix}:{ticker}" if prefix else ticker


def to_yf_symbol(ticker: str, exchange: Optional[str]) -> str:
    """
    Convert a bare ticker + exchange name into the Yahoo Finance symbol.

        TSX  + XEQT  →  XEQT.TO
        VENTURE + GPV  →  GPV.V
        NASDAQ + QQQ  →  QQQ
        unknown       →  ticker as-is
    """
    if not exchange:
        return ticker
    suffix = EXCHANGE_TO_YF_SUFFIX.get(exchange.upper())
    if suffix is None:
        return ticker          # unknown exchange — try as-is
    return f"{ticker}{suffix}"


def fetch_etf_sectors(yf_symbol: str) -> Optional[dict]:
    """
    Use yfinance to get sector weightings for one ETF symbol.

    Returns a dict {sector_name: weight_float} or None if no data found.
    Sector names are title-cased; weights are raw decimals (0–1).
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(yf_symbol)
        fd = ticker.funds_data

        # funds_data.sector_weightings: list of {sectorKey: weight, ...} dicts
        # or a dict of {sectorKey: weight} directly, depending on yfinance version
        raw = fd.sector_weightings
        if raw is None:
            return None

        # yfinance ≥ 0.2.x returns a list of dicts; older versions return a flat dict
        if isinstance(raw, list):
            merged: dict = {}
            for entry in raw:
                merged.update(entry)
            raw = merged

        if not raw:
            return None

        # Normalise keys to Title Case
        return {k.replace("_", " ").title(): float(v) for k, v in raw.items() if v is not None}

    except Exception:
        return None


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def sheets_service():
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials

    if not GOOGLE_CREDS.exists():
        raise FileNotFoundError(f"Service account key not found: {GOOGLE_CREDS}")
    creds = Credentials.from_service_account_file(str(GOOGLE_CREDS), scopes=SHEETS_API_SCOPE)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def load_spreadsheet_id() -> str:
    if not SHEETS_CONFIG.exists():
        raise FileNotFoundError(f"Sheets config not found: {SHEETS_CONFIG}")
    sid = json.loads(SHEETS_CONFIG.read_text()).get("spreadsheet_id", "").strip()
    if not sid:
        raise ValueError(f"'spreadsheet_id' is empty in {SHEETS_CONFIG}")
    return sid


def _is_etf_flag(value: str) -> bool:
    """Return True for any cell value that means 'yes, this is an ETF'."""
    return value.strip().upper() not in ("", "FALSE", "NO", "N", "0")


def get_etf_tickers(service, spreadsheet_id: str) -> list:
    """
    Read the Portfolio sheet and return a list of (ticker, exchange) pairs
    for every row whose ETF column has a truthy value.
    Deduplicates on (ticker, exchange).
    """
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{SHEET_NAME_PORTFOLIO}'")
        .execute()
    )
    rows = result.get("values", [])
    if not rows:
        return []

    header = [h.strip() for h in rows[0]]

    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    ticker_col   = col(PORTFOLIO_COL_TICKER)
    exchange_col = col(PORTFOLIO_COL_EXCHANGE)
    etf_col      = col(PORTFOLIO_COL_ETF)

    if ticker_col is None:
        raise ValueError(f"Column '{PORTFOLIO_COL_TICKER}' not found in Portfolio header.")
    if etf_col is None:
        raise ValueError(f"Column '{PORTFOLIO_COL_ETF}' not found in Portfolio header.")

    seen = set()
    results = []
    for row in rows[1:]:
        def cell(idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        etf_flag = cell(etf_col)
        if not _is_etf_flag(etf_flag):
            continue

        ticker   = cell(ticker_col)
        exchange = cell(exchange_col) if exchange_col is not None else None

        if not ticker:
            continue

        key = (ticker, exchange or "")
        if key not in seen:
            seen.add(key)
            results.append((ticker, exchange or None))

    return results


def ensure_etf_sheet(service, spreadsheet_id: str) -> int:
    """Return the sheetId of 'ETF Sectors', creating it if it doesn't exist."""
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sheet in meta["sheets"]:
        if sheet["properties"]["title"] == SHEET_NAME_ETF:
            return sheet["properties"]["sheetId"]

    resp = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME_ETF}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def clear_and_write(service, spreadsheet_id: str, rows: list):
    """Clear 'ETF Sectors' and write rows (header first)."""
    values_api = service.spreadsheets().values()
    values_api.clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME_ETF}'",
        body={},
    ).execute()
    values_api.update(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME_ETF}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()


# ── Main ───────────────────────────────────────────────────────────────────────

def main(cli_tickers: Optional[list] = None):
    service        = sheets_service()
    spreadsheet_id = load_spreadsheet_id()

    # Build the list of (ticker, exchange) to fetch
    if cli_tickers:
        from portfolio_updater import extract_exchange_and_ticker
        ticker_list = []
        for raw in cli_tickers:
            exchange, ticker = extract_exchange_and_ticker(raw)
            ticker_list.append((ticker, exchange))
    else:
        print(f"[etf_sectors] Reading ETF tickers from '{SHEET_NAME_PORTFOLIO}' sheet…")
        ticker_list = get_etf_tickers(service, spreadsheet_id)
        print(f"              {len(ticker_list)} ETF row(s) found")

    if not ticker_list:
        print("[etf_sectors] No ETF tickers to fetch.")
        return

    # Fetch sector data
    # results: yf_symbol → {"gf_symbol": str, "sectors": {sector: weight}}
    results     = {}
    all_sectors = set()
    import_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("[etf_sectors] Fetching sector data via yfinance…")
    for i, (ticker, exchange) in enumerate(ticker_list):
        yf_symbol = to_yf_symbol(ticker, exchange)
        gf_symbol = to_gf_symbol(ticker, exchange)
        print(f"  [{i+1}/{len(ticker_list)}] {yf_symbol:<14}", end=" ", flush=True)

        sectors = fetch_etf_sectors(yf_symbol)

        if sectors is None:
            print("skipped (no sector data returned)")
        else:
            results[yf_symbol] = {"gf_symbol": gf_symbol, "sectors": sectors}
            all_sectors.update(sectors.keys())
            top = ", ".join(
                f"{k}: {v:.1%}" for k, v in sorted(sectors.items(), key=lambda x: -x[1])[:3]
            )
            print(f"ok  ({top}…)")

        if i < len(ticker_list) - 1:
            time.sleep(REQUEST_INTERVAL_S)

    if not results:
        print("[etf_sectors] No ETF data retrieved — nothing written.")
        return

    sorted_sectors = sorted(all_sectors)
    header    = ["Symbol", "GF Symbol", "Import Date"] + sorted_sectors
    data_rows = [
        [sym, entry["gf_symbol"], import_date] + [entry["sectors"].get(s, "") for s in sorted_sectors]
        for sym, entry in sorted(results.items())
    ]

    print(f"\n[etf_sectors] Writing {len(data_rows)} ETF(s) × {len(sorted_sectors)} sector(s) to '{SHEET_NAME_ETF}'…")
    ensure_etf_sheet(service, spreadsheet_id)
    clear_and_write(service, spreadsheet_id, [header] + data_rows)
    print(f"[etf_sectors] Done. '{SHEET_NAME_ETF}' sheet updated.\n")


if __name__ == "__main__":
    cli_tickers = [t.upper() for t in sys.argv[1:]] if len(sys.argv) > 1 else None
    main(cli_tickers)
