"""
portfolio_updater.py

Updates the 'Portfolio' sheet with the latest Import Date and Quantity from
the pipeline processed data via the Google Sheets API v4.

Requires:
  ~/.config/portfolio_pipeline/google_credentials.json   (service account key)
  ~/.config/portfolio_pipeline/sheets_config.json        ({"spreadsheet_id": "…"})

New holdings found in the pipeline but not yet in the sheet are appended at
the bottom with Ticker, Import Date, Quantity, Currency, and Account
pre-filled. All other columns are left blank for manual entry.

What IS updated (existing WS / QT rows only):
    - Column C  Import Date
    - Column D  Quantity

What is NOT touched:
    - All other columns (formulas, Asset Class, Region, Tags, etc.)
    - Non-WS and non-QT rows (private equity, crypto, Computershare, etc.)
    - WS cash positions (not included in the Wealthsimple holdings export)
"""

import json
from pathlib import Path
from typing import Optional
import pandas as pd


# ── Configuration ──────────────────────────────────────────────────────────────

PROCESSED_DIR    = Path(__file__).parent / "data" / "processed"
SHEET_NAME       = "Portfolio"
CONFIG_DIR       = Path.home() / ".config" / "portfolio_pipeline"
GOOGLE_CREDS     = CONFIG_DIR / "google_credentials.json"
SHEETS_CONFIG    = CONFIG_DIR / "sheets_config.json"
SHEETS_API_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]

# Pipeline account_type value → Portfolio sheet Account label
WS_ACCOUNT_MAP = {"FHSA": "WS - FHSA", "RRSP": "WS - RRSP", "TFSA": "WS - TFSA"}
QT_ACCOUNT_MAP = {"TFSA": "QT - TFSA"}

# Normalise Wealthsimple exchange names to Google Finance exchange codes.
# Google Finance uses TSE for the Toronto Stock Exchange and CVE for TSX Venture.
WS_EXCHANGE_MAP = {
    "TSX":          "TSE",
    "TSX-V":        "CVE",
    "NYSE":         "NYSE",
    "NYSE ARCA":    "NYSEARCA",
    "NASDAQ":       "NASDAQ",
    "BATS":         "BATS",
    "CBOE CANADA":  "TSE",   # CBOE Canada equities trade on TSX infrastructure
}

# Aliases that may appear in the Portfolio sheet → canonical Google Finance code.
# Applied when reading the sheet so that legacy values (TSX, VENTURE, etc.) still match.
EXCHANGE_ALIASES = {
    "TSX":      "TSE",
    "TSE":      "TSE",
    "TSX-V":    "CVE",
    "VENTURE":  "CVE",
    "CVE":      "CVE",
    "NYSE":     "NYSE",
    "NASDAQ":   "NASDAQ",
    "NYSEARCA": "NYSEARCA",
    "BATS":     "BATS",
}

# Column names as they appear in the Portfolio table header row
REQUIRED_COLUMNS = {"Exchange", "Ticker", "Import Date", "Quantity", "Account", "Currency"}


# ── Ticker normalisation ───────────────────────────────────────────────────────

def extract_exchange_and_ticker(raw) -> tuple:
    """
    Extract exchange and normalized ticker from a raw ticker string.

    Returns (exchange, norm_ticker) tuple.

    Examples:
        'TSE:HLIT'    → ('TSE', 'HLIT')
        'NASDAQ:MSFT' → ('NASDAQ', 'MSFT')
        'VVSG.TO'     → ('TSE', 'VVSG')
        'GPV.V'       → ('CVE', 'GPV')
        'MSFT'        → ('NASDAQ', 'MSFT')  (default to US exchange)
        None / ''     → (None, None)

    Exchange values are always returned as Google Finance codes (TSE, CVE, …).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None

    t = str(raw).strip()
    if not t:
        return None, None

    exchange = None

    # Extract exchange prefix  (e.g. 'TSE:', 'NASDAQ:', 'NYSE:', 'CVE:')
    if ":" in t:
        exchange = t.split(":")[0].upper()
        t = t.split(":")[-1]

    # Extract exchange suffix and infer exchange  (e.g. '.TO', '.TSX', '.V')
    for suffix in (".TO", ".TSX", ".TSE", ".V"):
        if t.upper().endswith(suffix):
            if not exchange:
                exchange = "TSE" if suffix in (".TO", ".TSX", ".TSE") else "CVE"
            t = t[: -len(suffix)]
            break

    # Default to NASDAQ for US tickers if no exchange found
    if not exchange:
        exchange = "NASDAQ"

    # Normalise any alias (e.g. TSX → TSE) that may have come from a prefix
    exchange = normalize_exchange(exchange) or exchange

    norm_ticker = t.upper() or None
    return exchange, norm_ticker


def normalize_exchange(raw: Optional[str]) -> Optional[str]:
    """
    Map any exchange alias to its canonical Google Finance code.

        'TSX'     → 'TSE'
        'VENTURE' → 'CVE'
        'TSX-V'   → 'CVE'
        'TSE'     → 'TSE'   (already canonical)
        None/''   → None
    """
    if not raw:
        return None
    return EXCHANGE_ALIASES.get(raw.strip().upper(), raw.strip().upper())


def normalize_ticker(raw) -> Optional[str]:
    """
    Normalize a ticker by stripping exchange prefixes/suffixes.
    (Kept for backwards compatibility; use extract_exchange_and_ticker for full info.)
    """
    _, ticker = extract_exchange_and_ticker(raw)
    return ticker


# ── Pipeline data loading ──────────────────────────────────────────────────────

def infer_qt_currency(symbol: str) -> str:
    """
    Derive currency from a QT symbol.
    The Questrade API does not return currency per position.

    'CASH-CAD' → 'CAD', 'CASH-USD' → 'USD'
    'VVSG.TO'  → 'CAD'  (TSX-listed)
    'MSFT'     → 'USD'  (US-listed, default)
    """
    s = str(symbol).upper()
    if s.startswith("CASH-"):
        return s.split("-")[-1]
    if s.endswith(".TO") or s.endswith(".TSX") or s.endswith(".V"):
        return "CAD"
    return "USD"


def load_pipeline_data() -> pd.DataFrame:
    """
    Load WS holdings and QT positions and combine into a single DataFrame
    with columns: symbol, norm_symbol, exchange, quantity, import_date,
                  portfolio_account, currency.

    Rows whose account_type has no mapping (unknown accounts) are dropped.
    """
    ws = pd.read_csv(PROCESSED_DIR / "ws_holdings.csv")
    qt = pd.read_csv(PROCESSED_DIR / "qt_positions.csv")

    ws["portfolio_account"] = ws["account_type"].map(WS_ACCOUNT_MAP)
    qt["portfolio_account"] = qt["account_type"].map(QT_ACCOUNT_MAP)

    # WS already provides an exchange column — normalise it to Portfolio sheet values.
    ws_slim = (
        ws[["symbol", "quantity", "import_date", "portfolio_account", "market_price_currency", "exchange"]]
        .rename(columns={"market_price_currency": "currency"})
        .copy()
    )
    ws_slim["exchange"] = ws_slim["exchange"].map(
        lambda x: WS_EXCHANGE_MAP.get(str(x).strip(), str(x).strip()) if pd.notna(x) else None
    )
    ws_slim["norm_symbol"] = ws_slim["symbol"].apply(lambda s: extract_exchange_and_ticker(s)[1])

    # QT has no exchange column — infer exchange from the ticker symbol (e.g. VVSG.TO → TSX).
    qt["currency"] = qt["symbol"].apply(infer_qt_currency)
    qt_slim = qt[["symbol", "quantity", "import_date", "portfolio_account", "currency"]].copy()
    qt_slim[["exchange", "norm_symbol"]] = qt_slim["symbol"].apply(
        lambda s: pd.Series(extract_exchange_and_ticker(s))
    )

    combined = pd.concat([ws_slim, qt_slim], ignore_index=True)
    combined = combined.dropna(subset=["portfolio_account"])
    return combined


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def _col_letter(n: int) -> str:
    """Convert a 1-based column number to an A1-notation letter (1→A, 26→Z, 27→AA)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _cell(row: int, col: int) -> str:
    """Return A1 notation for a 1-based (row, col) pair, e.g. (5, 3) → 'C5'."""
    return f"{_col_letter(col)}{row}"


def _sheet_val(row_list: list, col_1based: int) -> str:
    """Safely read a 0-indexed list using a 1-based column number."""
    idx = col_1based - 1
    return row_list[idx] if idx < len(row_list) else ""


def resolve_columns(header_row: list) -> dict:
    """
    Build a column-name → 1-based index mapping from the sheet header row.
    Raises ValueError listing any required columns that are missing.
    """
    cols = {name.strip(): i + 1 for i, name in enumerate(header_row)}
    missing = REQUIRED_COLUMNS - cols.keys()
    if missing:
        raise ValueError(
            f"Missing required column(s) in '{SHEET_NAME}' header: {', '.join(sorted(missing))}\n"
            f"Found columns: {list(cols.keys())}"
        )
    return cols


def build_lookups_from_values(all_values: list, cols: dict) -> tuple:
    """
    Scan the Portfolio sheet and return two lookup dicts:

      security_lookup : (norm_ticker, account, exchange) → 1-based sheet row number
      cash_lookup     : (account, currency)              → 1-based sheet row number

    If the Exchange column is empty, infers exchange from the ticker.
    The first occurrence of each key wins.
    """
    security_lookup = {}
    cash_lookup     = {}

    for i, row in enumerate(all_values[1:], start=2):   # row 1 = header → skip
        ticker   = _sheet_val(row, cols["Ticker"])
        account  = _sheet_val(row, cols["Account"])
        exchange = _sheet_val(row, cols["Exchange"]).strip().upper()
        currency = _sheet_val(row, cols["Currency"])

        if not account:
            continue
        account = str(account).strip()

        _, norm = extract_exchange_and_ticker(ticker)
        if norm:
            raw_ex = exchange if exchange else extract_exchange_and_ticker(ticker)[0]
            exchange_str = normalize_exchange(raw_ex) or raw_ex
            key = (norm, account, exchange_str)
            if key not in security_lookup:
                security_lookup[key] = i
        elif currency:
            key = (account, str(currency).strip().upper())
            if key not in cash_lookup:
                cash_lookup[key] = i

    return security_lookup, cash_lookup


def build_batch_updates(pipeline_df: pd.DataFrame,
                        security_lookup: dict, cash_lookup: dict,
                        all_values: list, cols: dict) -> tuple:
    """
    Walk the pipeline DataFrame and produce:
      batch_data : list of {"range": "C5:D5", "values": [[date, qty]]} dicts
                   ready for spreadsheets.values().batchUpdate()
      new_rows   : list of row dicts for holdings not yet in the sheet

    Matching strategy (in order):
      1. Direct match by (normalized_ticker, account, exchange)
      2. Fallback: scan sheet rows for normalized ticker + account + exchange match
    """
    batch_data = []
    new_rows   = []

    for _, row in pipeline_df.iterrows():
        symbol   = str(row["symbol"])
        norm     = row["norm_symbol"]
        exchange = row["exchange"]
        account  = row["portfolio_account"]
        quantity = float(row["quantity"])
        date_str = str(row["import_date"])
        currency = str(row["currency"]).upper() if pd.notna(row.get("currency")) else ""

        if symbol.startswith("CASH-"):
            row_num = cash_lookup.get((account, currency))
        else:
            row_num = security_lookup.get((norm, account, exchange)) if norm else None

        # If direct lookup fails, scan sheet rows for a match
        if not row_num and norm:
            for i, sheet_row in enumerate(all_values[1:], start=2):
                sheet_ticker   = _sheet_val(sheet_row, cols["Ticker"])
                sheet_account  = _sheet_val(sheet_row, cols["Account"])
                sheet_exchange = _sheet_val(sheet_row, cols["Exchange"]).strip().upper()

                _, sheet_norm = extract_exchange_and_ticker(sheet_ticker)
                raw_sheet_ex = sheet_exchange if sheet_exchange else extract_exchange_and_ticker(sheet_ticker)[0]
                sheet_ex = normalize_exchange(raw_sheet_ex) or raw_sheet_ex

                if sheet_norm == norm and sheet_account.strip() == account and sheet_ex == exchange:
                    row_num = i
                    break

        if row_num:
            range_str = f"{_cell(row_num, cols['Import Date'])}:{_cell(row_num, cols['Quantity'])}"
            batch_data.append({"range": range_str, "values": [[date_str, quantity]]})
        else:
            new_rows.append(row.to_dict())

    return batch_data, new_rows


def build_append_rows(new_rows: list, num_cols: int, cols: dict) -> list:
    """
    Convert a list of new-holding dicts into zero-padded row lists suitable
    for spreadsheets.values().append().
    """
    rows = []
    for row in new_rows:
        symbol  = str(row["symbol"])
        account = row["portfolio_account"]
        exchange = row.get("exchange", "")
        ticker_value = "" if symbol.startswith("CASH-") else (row["norm_symbol"] or "")

        r = [""] * num_cols
        r[cols["Ticker"] - 1]      = ticker_value
        r[cols["Import Date"] - 1] = str(row["import_date"])
        r[cols["Quantity"] - 1]    = float(row["quantity"])
        r[cols["Currency"] - 1]    = str(row["currency"]).upper()
        r[cols["Account"] - 1]     = account
        r[cols["Exchange"] - 1]    = exchange
        rows.append(r)
    return rows


# ── Main (Google Sheets mode) ──────────────────────────────────────────────────

def main_sheets():
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials

    # ── Load config ─────────────────────────────────────────────────────────
    if not GOOGLE_CREDS.exists():
        raise FileNotFoundError(
            f"Service account key not found: {GOOGLE_CREDS}\n"
            "See the setup section in the docstring for instructions."
        )
    if not SHEETS_CONFIG.exists():
        raise FileNotFoundError(
            f"Sheets config not found: {SHEETS_CONFIG}\n"
            'Create it with: {"spreadsheet_id": "<your-sheet-id>"}'
        )
    with open(SHEETS_CONFIG) as f:
        cfg = json.load(f)
    spreadsheet_id = cfg.get("spreadsheet_id", "").strip()
    if not spreadsheet_id:
        raise ValueError(f"'spreadsheet_id' is empty in {SHEETS_CONFIG}")

    # ── Authenticate ─────────────────────────────────────────────────────────
    creds   = Credentials.from_service_account_file(str(GOOGLE_CREDS), scopes=SHEETS_API_SCOPE)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values_api = service.spreadsheets().values()

    sheet_range = f"'{SHEET_NAME}'"
    print(f"[portfolio] Reading sheet '{SHEET_NAME}' from Google Sheets…")
    result     = values_api.get(spreadsheetId=spreadsheet_id, range=sheet_range).execute()
    all_values = result.get("values", [])
    if not all_values:
        raise ValueError("Sheet appears to be empty — nothing to update.")

    num_cols = max(len(r) for r in all_values)
    cols     = resolve_columns(all_values[0])
    print(f"            {len(all_values) - 1} data rows, {num_cols} columns")

    # ── Index existing rows ──────────────────────────────────────────────────
    print("[portfolio] Indexing Portfolio sheet…")
    security_lookup, cash_lookup = build_lookups_from_values(all_values, cols)
    print(f"            {len(security_lookup)} security rows, {len(cash_lookup)} cash rows indexed")

    # ── Load pipeline data ───────────────────────────────────────────────────
    print("[portfolio] Loading pipeline data…")
    pipeline_df = load_pipeline_data()
    print(f"            {len(pipeline_df)} rows (WS holdings + QT positions)")

    # ── Build updates ────────────────────────────────────────────────────────
    print("[portfolio] Matching and updating…")
    batch_data, new_rows = build_batch_updates(pipeline_df, security_lookup, cash_lookup, all_values, cols)

    if batch_data:
        # Prepend sheet name to ranges so the API knows which sheet to update
        batch_data_with_sheet = [
            {**entry, "range": f"'{SHEET_NAME}'!{entry['range']}"}
            for entry in batch_data
        ]
        values_api.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": batch_data_with_sheet},
        ).execute()
        print(f"\n  Updated {len(batch_data)} existing row(s):")
        for entry in batch_data:
            print(f"    {entry['range']:<10}  {entry['values'][0][0]}")
    else:
        print("\n  No existing rows to update.")

    if new_rows:
        append_rows = build_append_rows(new_rows, num_cols, cols)
        values_api.append(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": append_rows},
        ).execute()
        print(f"\n  Appended {len(append_rows)} new holding(s):")
        for row in new_rows:
            sym  = row["norm_symbol"] or row["symbol"]
            acct = row["portfolio_account"]
            print(f"    {sym:<20}  {acct}")
    else:
        print("\n  No new holdings to add.")

    print("\n[portfolio] Google Sheet updated successfully.\n")


if __name__ == "__main__":
    main_sheets()
