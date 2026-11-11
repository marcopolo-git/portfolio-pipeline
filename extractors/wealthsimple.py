"""
Wealthsimple CSV ingestion extractor.

Wealthsimple does not provide an official API for retail investors.
The most reliable extraction method is a manual CSV export from the web app:
  - Holdings: App > Portfolio > Holdings report (top-right icon)

Place the exported file in data/raw/wealthsimple/ and run this module.
Files are matched by prefix: WS_holdings-*.csv (the most recent one is used).
"""

import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd


RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "wealthsimple"
PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"

# Metadata footer pattern emitted by Wealthsimple exports
_FOOTER_PATTERN = re.compile(r"As of \d{4}-\d{2}-\d{2}", re.IGNORECASE)


def _file_sort_key(path: Path) -> tuple:
    """
    Sort key for picking the most recent file.

    Prefers the date found in the filename (YYYY-MM-DD).
    Falls back to last-modified time if no date is in the name,
    and prints a warning when it does so.
    Returns a (source_rank, datetime) tuple so filename-dated files
    always sort above mtime-only files.
    """
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if match:
        from datetime import date
        return (1, date.fromisoformat(match.group(1)))
    else:
        print(
            f"[wealthsimple] Warning: no date found in filename '{path.name}', "
            "falling back to last-modified time for ordering."
        )
        from datetime import datetime, timezone
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
        return (0, mtime)


def _latest_file(pattern: str) -> Path:
    """Return the file with the most recent date in its filename, matching a glob pattern."""
    matches = list(RAW_DIR.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No file matching '{pattern}' found in {RAW_DIR}.\n"
            "Export the file from Wealthsimple and place it in that directory."
        )
    matches.sort(key=_file_sort_key, reverse=True)
    if len(matches) > 1:
        print(f"[wealthsimple] Multiple files match '{pattern}', using most recent: {matches[0].name}")
    return matches[0]


def _read_csv_strip_footer(path: Path) -> pd.DataFrame:
    """
    Read a Wealthsimple CSV, dropping the metadata footer rows.

    Wealthsimple appends a row like:
        "As of 2026-04-03 10:54 GMT-07:00"
    and a blank trailing line. Both are dropped here.
    """
    raw = pd.read_csv(path, dtype=str, skip_blank_lines=False)

    # Drop rows where the first column contains the "As of" footer
    first_col = raw.columns[0]
    mask = raw[first_col].str.contains(_FOOTER_PATTERN, na=False)
    dropped = mask.sum()
    if dropped:
        print(f"[wealthsimple] Stripped {dropped} footer row(s) from {path.name}")
    df = raw[~mask].dropna(how="all").reset_index(drop=True)
    return df


def extract_holdings(path: Optional[Path] = None) -> pd.DataFrame:
    """
    Ingest a Wealthsimple holdings report CSV.

    Returns a normalized DataFrame with clean column names and typed values.
    Columns: account_name, account_type, account_classification, account_number,
             symbol, exchange, name, security_type, quantity, direction,
             market_price, market_price_currency, book_value_cad,
             market_value_cad, unrealized_return_cad
    """
    path = path or _latest_file("WS_holdings-*.csv")
    print(f"[wealthsimple] Reading holdings from: {path.name}")

    df = _read_csv_strip_footer(path)

    # Normalize column names: lowercase, replace spaces/special chars with underscores
    df.columns = (
        df.columns.str.strip()
        .str.lower()
        .str.replace(r"[\s\(\)/]+", "_", regex=True)
        .str.replace(r"_+$", "", regex=True)
    )

    # Select and rename to stable internal names
    rename = {
        "account_name": "account_name",
        "account_type": "account_type",
        "account_classification": "account_classification",
        "account_number": "account_number",
        "symbol": "symbol",
        "exchange": "exchange",
        "name": "name",
        "security_type": "security_type",
        "quantity": "quantity",
        "position_direction": "direction",
        "market_price": "market_price",
        "market_price_currency": "market_price_currency",
        "book_value_cad": "book_value_cad",
        "market_value": "market_value",
        "market_unrealized_returns": "unrealized_return",
    }
    # Keep only columns that exist (guards against future export format changes)
    available = {k: v for k, v in rename.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available)

    # Type conversions
    numeric_cols = ["quantity", "market_price", "book_value", "market_value", "unrealized_return"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Strip stray quotes from string columns
    str_cols = df.select_dtypes(include="object").columns
    df[str_cols] = df[str_cols].apply(lambda s: s.str.strip('"').str.strip())

    # Import date from filename, e.g. WS_holdings-report-2026-04-03.csv → 2026-04-03
    match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    df["import_date"] = match.group() if match else None

    print(f"[wealthsimple] Holdings: {len(df)} positions across {df['account_number'].nunique()} accounts")
    return df


def save(df: pd.DataFrame, filename: str) -> Path:
    """Write a DataFrame to the processed directory as CSV."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / filename
    df.to_csv(out, index=False)
    print(f"[wealthsimple] Saved {len(df)} rows → {out}")
    return out


def run(holdings_path: Optional[Path] = None) -> dict:
    """
    Full Wealthsimple ingestion pass: reads the latest holdings export,
    normalizes it, and writes data/processed/ws_holdings.csv.
    Returns a dict with the holdings DataFrame.
    """
    holdings = extract_holdings(holdings_path)
    save(holdings, "ws_holdings.csv")
    return {"holdings": holdings}


if __name__ == "__main__":
    # Allow passing the file path as a CLI argument for one-off runs:
    #   python -m extractors.wealthsimple path/to/holdings.csv
    args = sys.argv[1:]
    run(holdings_path=Path(args[0]) if args else None)
