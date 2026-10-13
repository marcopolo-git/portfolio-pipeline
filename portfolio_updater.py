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

Holdings the broker no longer reports (fully sold) get Quantity 0 instead of
keeping their last quantity. See build_sold_updates() for the safeguards.

What is NOT touched:
    - All other columns (formulas, Asset Class, Region, Tags, etc.)
    - Non-WS and non-QT rows (private equity, crypto, Computershare, etc.)

Cash (WS CURRENCY rows and QT balances, both as CASH-<currency>) is matched to
sheet rows with a blank Ticker by (Account, Currency).
"""

import json
from datetime import date, datetime, timedelta
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
SHEETS_EPOCH     = date(1899, 12, 30)   # day 0 of Google Sheets date serial numbers

# Pipeline account_type value → Portfolio sheet Account label
WS_ACCOUNT_MAP = {"FHSA": "WS - FHSA", "RRSP": "WS - RRSP", "TFSA": "WS - TFSA"}
QT_ACCOUNT_MAP = {"TFSA": "QT - TFSA"}

# Holdings maintained by hand in the sheet (ticker, account) — never updated, appended
# or zeroed. Use the ticker as the broker reports it AND as it appears in the sheet if
# they differ. Kept manually because of Google Finance issues; see README.
MANUAL_HOLDINGS = {
    ("BIP",    "WS - TFSA"),   # sheet ticker
    ("BIP.UN", "WS - TFSA"),   # Wealthsimple ticker
    ("GLDM",   "WS - RRSP"),   # WS reports NYSE, sheet uses NYSEARCA
    ("VTI",    "WS - RRSP"),   # WS reports NYSE, sheet uses NYSEARCA
}

# Normalise Wealthsimple exchange names to Google Finance exchange codes.
# Google Finance uses TSE for the Toronto Stock Exchange and CVE for TSX Venture.
WS_EXCHANGE_MAP = {
    "TSX":          "TSE",
    "TSX-V":        "CVE",
    "NYSE":         "NYSE",
    "NYSE ARCA":    "NYSEARCA",
    "ARCA":         "NYSEARCA",
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
    "TSXV":     "CVE",
    "VENTURE":  "CVE",
    "CVE":      "CVE",
    "NYSE":     "NYSE",
    "NASDAQ":   "NASDAQ",
    "NYSEARCA": "NYSEARCA",
    "ARCA":     "NYSEARCA",
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
        'ARCA:GLDM'   → ('NYSEARCA', 'GLDM')
        'MSFT'        → (None, 'MSFT')      (exchange unknown — left blank)
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

    # Normalise any alias (e.g. TSX → TSE) that may have come from a prefix.
    # No prefix or suffix → exchange stays None (blank); it is not guessed.
    exchange = normalize_exchange(exchange)

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
    # WS exports cash as security_type CURRENCY with the bare currency as its symbol (e.g. 'CAD').
    # Rename to 'CASH-CAD' so it is matched to sheet cash rows the same way QT cash is.
    is_cash = ws["security_type"].eq("CURRENCY")
    ws_slim.loc[is_cash, "symbol"] = "CASH-" + ws_slim.loc[is_cash, "currency"].str.upper()
    ws_slim["norm_symbol"] = ws_slim["symbol"].apply(lambda s: extract_exchange_and_ticker(s)[1])

    # QT exchange comes from the API's listingExchange (e.g. ARCA → NYSEARCA, TSX → TSE).
    # Unmapped codes pass through unchanged so new ones show up in the sheet; if the CSV
    # has no listing exchange, fall back to the ticker suffix (e.g. VVSG.TO → TSE), else blank.
    qt["currency"] = qt["symbol"].apply(infer_qt_currency)
    qt_slim = qt[["symbol", "quantity", "import_date", "portfolio_account", "currency"]].copy()
    qt_slim[["exchange", "norm_symbol"]] = qt_slim["symbol"].apply(
        lambda s: pd.Series(extract_exchange_and_ticker(s))
    )
    if "listing_exchange" in qt.columns:
        listed = qt["listing_exchange"].map(lambda x: normalize_exchange(x) if pd.notna(x) else None)
        qt_slim["exchange"] = listed.where(listed.notna(), qt_slim["exchange"])

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
      2. Fallback: scan sheet rows for normalized ticker + account + exchange match.
         A blank exchange on either side means "unknown" and matches any exchange
         (e.g. QT positions, which carry no exchange, match a sheet row set to NASDAQ).
    """
    batch_data   = []
    new_rows     = []
    matched_rows = set()

    for _, row in pipeline_df.iterrows():
        symbol   = str(row["symbol"])
        norm     = row["norm_symbol"]
        exchange = row["exchange"] if pd.notna(row["exchange"]) else None
        account  = row["portfolio_account"]
        quantity = float(row["quantity"])
        date_str = str(row["import_date"])
        currency = str(row["currency"]).upper() if pd.notna(row.get("currency")) else ""

        if (norm, account) in MANUAL_HOLDINGS:
            continue

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

                exchange_ok = not exchange or not sheet_ex or sheet_ex == exchange
                if sheet_norm == norm and sheet_account.strip() == account and exchange_ok:
                    row_num = i
                    break

        if row_num:
            range_str = f"{_cell(row_num, cols['Import Date'])}:{_cell(row_num, cols['Quantity'])}"
            batch_data.append({"range": range_str, "values": [[date_str, quantity]]})
            matched_rows.add(row_num)
        else:
            new_rows.append(row.to_dict())

    return batch_data, new_rows, matched_rows


def _as_date(value) -> Optional[date]:
    """Unformatted sheet date (serial number, or 'YYYY-MM-DD' text) → date; None if unreadable."""
    if isinstance(value, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(value))
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def build_sold_updates(pipeline_df: pd.DataFrame, matched_rows: set,
                       raw_values: list, cols: dict) -> tuple:
    """
    Find WS / QT security rows the brokers no longer report (fully sold) and set
    their Quantity to 0. Brokers drop sold positions instead of reporting 0, so
    without this those rows keep their last quantity forever.

    raw_values must be read with UNFORMATTED_VALUE / SERIAL_NUMBER so Quantity and
    Import Date come back as numbers rather than locale-formatted text.

    A row is zeroed only when all of these hold:
      - its Account has data in this run (other accounts are never touched)
      - that data is at least as recent as the row's Import Date, so a stale CSV
        (e.g. a failed Questrade refresh) can't zero a newer holding
      - it has a Ticker (cash rows are left alone)
      - it wasn't matched, and its ticker isn't held in that account at all
        (so an exchange mismatch never zeroes a holding you still own)
      - its Quantity isn't already 0 or blank
      - it isn't in MANUAL_HOLDINGS

    Returns (batch_data, sold) where sold is a list of (ticker, account, old_qty).
    """
    account_dates = pipeline_df.groupby("portfolio_account")["import_date"].max().to_dict()
    held = set(zip(pipeline_df["norm_symbol"], pipeline_df["portfolio_account"]))

    batch_data = []
    sold       = []
    for i, row in enumerate(raw_values[1:], start=2):
        if i in matched_rows:
            continue
        account = str(_sheet_val(row, cols["Account"])).strip()
        ticker  = str(_sheet_val(row, cols["Ticker"])).strip()
        qty     = _sheet_val(row, cols["Quantity"])
        if account not in account_dates or not ticker or qty in ("", 0):
            continue
        norm = extract_exchange_and_ticker(ticker)[1]
        if (norm, account) in held or (norm, account) in MANUAL_HOLDINGS:
            continue
        data_date = str(account_dates[account])
        row_date  = _as_date(_sheet_val(row, cols["Import Date"]))
        if row_date and row_date > date.fromisoformat(data_date):
            continue

        range_str = f"{_cell(i, cols['Import Date'])}:{_cell(i, cols['Quantity'])}"
        batch_data.append({"range": range_str, "values": [[data_date, 0]]})
        sold.append((ticker, account, qty))

    return batch_data, sold


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
    batch_data, new_rows, matched_rows = build_batch_updates(
        pipeline_df, security_lookup, cash_lookup, all_values, cols
    )

    # Re-read unformatted so Quantity / Import Date are numbers, not locale text
    raw_values = values_api.get(
        spreadsheetId=spreadsheet_id, range=sheet_range,
        valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER",
    ).execute().get("values", [])
    sold_data, sold = build_sold_updates(pipeline_df, matched_rows, raw_values, cols)
    batch_data += sold_data

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

    if sold:
        print(f"\n  Zeroed {len(sold)} sold holding(s) no longer reported by the broker:")
        for ticker, acct, old_qty in sold:
            print(f"    {ticker:<20}  {acct:<12}  {old_qty} → 0")

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
