"""
Questrade API extractor.

Uses the official Questrade REST API (OAuth2 with refresh tokens).

SETUP (one-time, ~5 minutes):
  1. Log into Questrade at questrade.com
  2. Go to: My Account > Security > Apps > Authorize a Personal App
     (Direct link: https://apphub.questrade.com/UI/UserApps.aspx)
  3. Copy the refresh token shown
  4. Create the credentials file:
       ~/.config/portfolio_pipeline/questrade_credentials.json
     with the following content:
       { "refresh_token": "<paste token here>" }

TOKEN LIFECYCLE:
  - Each refresh token is single-use: using it to get an access token
    also issues a NEW refresh token. This module automatically persists
    the new token back to questrade_credentials.json so you don't have
    to manually update it.
  - Access tokens expire after 30 minutes (handled transparently).
  - If authentication fails, re-generate a token at the API hub link above.

API REFERENCE:
  https://www.questrade.com/api/documentation
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests


RAW_DIR = Path(__file__).parent.parent / "data" / "raw" / "questrade"
PROCESSED_DIR = Path(__file__).parent.parent / "data" / "processed"
CREDENTIALS_FILE = Path.home() / ".config" / "portfolio_pipeline" / "questrade_credentials.json"

_TOKEN_URL = "https://login.questrade.com/oauth2/token"


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class QuestiontradeAuth:
    """
    Manages Questrade OAuth2 token exchange.

    The refresh token is read from and written back to:
        ~/.config/portfolio_pipeline/questrade_credentials.json
    """

    def __init__(self):
        self._access_token: Optional[str] = None
        self._api_server: Optional[str] = None
        self._expires_at: float = 0.0

    def _token_expired(self) -> bool:
        # Treat as expired 60 seconds before actual expiry for safety
        return datetime.now(timezone.utc).timestamp() >= (self._expires_at - 60)

    def _exchange_refresh_token(self, refresh_token: str) -> dict:
        resp = requests.post(
            _TOKEN_URL,
            params={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
        if resp.status_code == 400:
            raise ValueError(
                "Questrade refresh token rejected (400). "
                "Generate a new token at https://apphub.questrade.com/UI/UserApps.aspx "
                f"and update 'refresh_token' in {CREDENTIALS_FILE}."
            )
        resp.raise_for_status()
        return resp.json()

    def _persist_refresh_token(self, new_token: str) -> None:
        """Write the rotated refresh token back to questrade_credentials.json."""
        data = json.loads(CREDENTIALS_FILE.read_text()) if CREDENTIALS_FILE.exists() else {}
        data["refresh_token"] = new_token
        CREDENTIALS_FILE.write_text(json.dumps(data, indent=2) + "\n")

    def ensure_valid_token(self) -> None:
        """Refresh the access token if expired or not yet fetched."""
        if self._access_token and not self._token_expired():
            return

        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                f"Questrade credentials file not found: {CREDENTIALS_FILE}\n"
                "See the setup instructions at the top of extractors/questrade.py."
            )

        refresh_token = json.loads(CREDENTIALS_FILE.read_text()).get("refresh_token", "").strip()
        if not refresh_token:
            raise ValueError(
                f"'refresh_token' is missing or empty in {CREDENTIALS_FILE}.\n"
                "See the setup instructions at the top of extractors/questrade.py."
            )

        print("[questrade] Exchanging refresh token for access token...")
        data = self._exchange_refresh_token(refresh_token)

        self._access_token = data["access_token"]
        self._api_server = data["api_server"].rstrip("/")
        self._expires_at = datetime.now(timezone.utc).timestamp() + data["expires_in"]

        # The API issues a new refresh token on each exchange — persist it
        new_refresh = data.get("refresh_token", "")
        if new_refresh and new_refresh != refresh_token:
            self._persist_refresh_token(new_refresh)
            print("[questrade] Refresh token rotated and saved to questrade_credentials.json")

    @property
    def headers(self) -> dict:
        self.ensure_valid_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    @property
    def api_server(self) -> str:
        self.ensure_valid_token()
        return self._api_server


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

class QuestiontradeClient:
    """Thin wrapper around the Questrade REST API."""

    def __init__(self, auth: QuestiontradeAuth):
        self._auth = auth
        self._session = requests.Session()

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        url = f"{self._auth.api_server}/v1/{path.lstrip('/')}"
        resp = self._session.get(url, headers=self._auth.headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_accounts(self) -> list[dict]:
        """Return list of account objects."""
        return self._get("accounts")["accounts"]

    def get_positions(self, account_id: str) -> list[dict]:
        """Return current positions for an account."""
        return self._get(f"accounts/{account_id}/positions")["positions"]

    def get_balances(self, account_id: str) -> dict:
        """Return balance summary for an account."""
        return self._get(f"accounts/{account_id}/balances")

    def get_symbols(self, symbol_ids: List[int]) -> list[dict]:
        """Return symbol details (incl. listingExchange) for the given symbol IDs."""
        if not symbol_ids:
            return []
        ids = ",".join(str(i) for i in symbol_ids)
        return self._get("symbols", params={"ids": ids})["symbols"]

    def get_activities(
        self,
        account_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """
        Return activities for an account within a date range.

        Parameters
        ----------
        start_date, end_date : str
            ISO 8601 dates, e.g. "2024-01-01" (time defaults to midnight UTC).
            Questrade limits each request to a 30-day window.
        """
        # Questrade requires RFC 3339 timestamps
        start = f"{start_date}T00:00:00-00:00"
        end = f"{end_date}T00:00:00-00:00"
        return self._get(
            f"accounts/{account_id}/activities",
            params={"startTime": start, "endTime": end},
        ).get("activities", [])


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

import pandas as pd
from datetime import timedelta


def _date_windows(start: str, end: str, window_days: int = 28) -> list[tuple[str, str]]:
    """
    Split [start, end] into chunks of at most window_days days.

    Questrade's activity endpoint rejects windows > 30 days.
    Using 28 days provides a safety margin.
    """
    fmt = "%Y-%m-%d"
    cur = datetime.strptime(start, fmt)
    stop = datetime.strptime(end, fmt)
    windows = []
    while cur < stop:
        chunk_end = min(cur + timedelta(days=window_days), stop)
        windows.append((cur.strftime(fmt), chunk_end.strftime(fmt)))
        cur = chunk_end
    return windows


def extract_positions(client: QuestiontradeClient, accounts: List[dict]) -> pd.DataFrame:
    """Fetch current positions and cash balances for all accounts and return as a DataFrame."""
    rows = []
    cash_rows = []

    for acct in accounts:
        # Security positions
        positions = client.get_positions(acct["number"])
        for pos in positions:
            pos["account_number"] = acct["number"]
            pos["account_type"] = acct["type"]
            rows.append(pos)

        # Cash balances — one row per currency with a non-zero balance
        balances = client.get_balances(acct["number"])
        for bal in balances.get("perCurrencyBalances", []):
            cash = bal.get("cash", 0)
            if cash == 0:
                continue
            currency = bal["currency"]
            cash_rows.append({
                "account_number": acct["number"],
                "account_type": acct["type"],
                "symbol": f"CASH-{currency}",
                "symbol_id": None,
                "quantity": cash,
                "market_price": 1.0,
                "market_value": cash,
                "avg_entry_price": 1.0,
                "book_value": cash,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "currency": currency,
                "under_reorg": False,
                "listing_exchange": None,
            })

    # Positions don't include the exchange — look it up per symbol (e.g. 'ARCA', 'NASDAQ', 'TSX').
    symbol_ids = sorted({pos["symbolId"] for pos in rows if pos.get("symbolId")})
    listing = {s["symbolId"]: s.get("listingExchange") for s in client.get_symbols(symbol_ids)}
    for pos in rows:
        pos["listingExchange"] = listing.get(pos.get("symbolId"))

    if not rows and not cash_rows:
        print("[questrade] No positions found.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Rename camelCase API fields to snake_case
    rename = {
        "account_number": "account_number",
        "account_type": "account_type",
        "symbol": "symbol",
        "symbolId": "symbol_id",
        "description": "name",
        "openQuantity": "quantity",
        "currentPrice": "market_price",
        "currentMarketValue": "market_value",
        "averageEntryPrice": "avg_entry_price",
        "totalCost": "book_value",
        "openPnl": "unrealized_pnl",
        "closedPnl": "realized_pnl",
        "currency": "currency",
        "isUnderReorg": "under_reorg",
        "listingExchange": "listing_exchange",
    }
    available = {k: v for k, v in rename.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available)

    # Append cash rows, aligning to the same columns
    if cash_rows:
        df_cash = pd.DataFrame(cash_rows)[df.columns.tolist()]
        df = pd.concat([df, df_cash], ignore_index=True)

    df["import_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"[questrade] Positions: {len(df)} rows across {df['account_number'].nunique()} accounts ({len(cash_rows)} cash row(s))")
    return df


def extract_activities(
    client: QuestiontradeClient,
    accounts: list[dict],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    Fetch all activities for all accounts within the date range.

    Automatically paginates in 28-day windows to respect the API limit.
    """
    all_rows = []
    windows = _date_windows(start_date, end_date)
    print(f"[questrade] Fetching activities: {start_date} to {end_date} in {len(windows)} window(s)")

    for acct in accounts:
        acct_id = acct["number"]
        for win_start, win_end in windows:
            rows = client.get_activities(acct_id, win_start, win_end)
            for row in rows:
                row["account_number"] = acct_id
                row["account_type"] = acct["type"]
            all_rows.extend(rows)

    if not all_rows:
        print("[questrade] No activities found in date range.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    # Rename camelCase API fields to snake_case
    rename = {
        "account_number": "account_number",
        "account_type": "account_type",
        "tradeDate": "transaction_date",
        "settlementDate": "settlement_date",
        "action": "action",
        "type": "activity_type",
        "symbol": "symbol",
        "symbolId": "symbol_id",
        "description": "name",
        "currency": "currency",
        "quantity": "quantity",
        "price": "unit_price",
        "grossAmount": "gross_amount",
        "commission": "commission",
        "netAmount": "net_cash_amount",
    }
    available = {k: v for k, v in rename.items() if k in df.columns}
    df = df[list(available.keys())].rename(columns=available)

    # Parse dates
    for col in ["transaction_date", "settlement_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True).dt.date

    # Remove duplicate rows that can appear at window boundaries
    df = df.drop_duplicates()

    df["import_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(
        f"[questrade] Activities: {len(df)} rows | "
        f"date range: {df['transaction_date'].min()} to {df['transaction_date'].max()}"
    )
    return df


def save(df: pd.DataFrame, filename: str) -> Path:
    """Write a DataFrame to the processed directory as CSV."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / filename
    df.to_csv(out, index=False)
    print(f"[questrade] Saved {len(df)} rows → {out}")
    return out


def run(start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict:
    """
    Full Questrade extraction pass.

    Parameters
    ----------
    start_date : str, optional
        Start of activity history in YYYY-MM-DD format. Defaults to 2020-01-01.
    end_date : str, optional
        End date. Defaults to today.

    Returns
    -------
    dict with keys 'positions' and 'activities' (DataFrames).
    """
    start_date = start_date or "2020-01-01"
    end_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    auth = QuestiontradeAuth()
    client = QuestiontradeClient(auth)

    print("[questrade] Fetching accounts...")
    accounts = client.get_accounts()
    print(f"[questrade] Found {len(accounts)} account(s): {[a['number'] for a in accounts]}")

    # Save raw account metadata for reference
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "accounts.json").write_text(json.dumps(accounts, indent=2))

    positions = extract_positions(client, accounts)
    activities = extract_activities(client, accounts, start_date, end_date)

    save(positions, "qt_positions.csv")
    save(activities, "qt_activities.csv")

    return {"positions": positions, "activities": activities}


if __name__ == "__main__":
    args = sys.argv[1:]
    kwargs = {}
    if len(args) >= 1:
        kwargs["start_date"] = args[0]
    if len(args) >= 2:
        kwargs["end_date"] = args[1]
    run(**kwargs)
