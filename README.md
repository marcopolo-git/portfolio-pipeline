# portfolio-pipeline

Pulls holdings and activities from Wealthsimple (CSV exports) and Questrade (REST API),
normalises them into CSVs, and syncs them to a Google Sheet.

| Script | What it does |
|---|---|
| `pipeline.py` | Ingests Wealthsimple CSVs and Questrade API data into `data/processed/` |
| `portfolio_updater.py` | Updates the `Portfolio` sheet with the latest quantities and adds new holdings |
| `etf_sectors.py` | Fetches ETF sector weightings (yfinance) into an `ETF Sectors` sheet |

## Setup

### 1. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Credentials

Credentials live outside the repo, in `~/.config/portfolio_pipeline/`. Templates are in
[`config.example/`](config.example/):

```bash
mkdir -p ~/.config/portfolio_pipeline
cp -n config.example/*.json ~/.config/portfolio_pipeline/
```

`-n` skips files that already exist, so re-running it never overwrites your real credentials.
Then fill in each file:

| File | How to get it |
|---|---|
| `questrade_credentials.json` | Questrade > My Account > Security > Apps > Authorize a Personal App ([API hub](https://apphub.questrade.com/UI/UserApps.aspx)). Paste the refresh token. The pipeline rotates and saves new tokens automatically. |
| `google_credentials.json` | Google Cloud Console > create a service account > Keys > Add key > JSON. Replace the template with the downloaded file, and share your Google Sheet with the service account's `client_email` (Editor). |
| `sheets_config.json` | The sheet ID from your Google Sheet URL: `docs.google.com/spreadsheets/d/<spreadsheet_id>/edit` |
| `manual_holdings.json` | Optional. Holdings you maintain by hand; see "Manually maintained holdings" below. Replace or empty (`[]`) the placeholder entries. |

### 3. Wealthsimple exports

Wealthsimple has no public API. Export the CSVs from the web app:

- Activities: Activity > Download
- Holdings: Portfolio > Holdings report

Drop them in the project root or `data/raw/wealthsimple/`. `pipeline.py` moves them into place.

## Usage

```bash
.venv/bin/python pipeline.py              # Wealthsimple + Questrade
.venv/bin/python pipeline.py --ws-only    # Wealthsimple only (no credentials needed)
.venv/bin/python pipeline.py --qt-only    # Questrade only
.venv/bin/python pipeline.py --start 2023-01-01 --end 2024-12-31   # Limit Questrade activity history
.venv/bin/python portfolio_updater.py     # Push to the Portfolio sheet
.venv/bin/python etf_sectors.py           # Refresh ETF sector weightings (rows flagged in the ETF column)
.venv/bin/python etf_sectors.py QQQ XEQT  # ...or only the tickers given
```

Raw and processed data (`data/`) are git-ignored because they contain personal financial data.

## How the sheet update works

`portfolio_updater.py` only writes the **Import Date** and **Quantity** columns of Wealthsimple
(`WS - …`) and Questrade (`QT - …`) rows. Everything else in the sheet is left alone.

- **Existing holdings** are matched by ticker, account and exchange, and updated. A blank
  exchange on either side matches any exchange.
- **New holdings** are appended at the bottom with the other columns blank, for you to fill in.
- **Sold holdings** (no longer reported by the broker) get Quantity `0`. The row is kept, not
  deleted. This only happens for accounts the broker reported in this run, and only when the
  row's Import Date is readable and not newer than that data, so stale or failed exports never
  zero a holding.
- **Cash** is matched to rows with a blank Ticker by account and currency.
- **Exchanges** come from the Wealthsimple export and from Questrade's listing exchange (e.g.
  Questrade `ARCA` → `NYSEARCA`, `TSX` → `TSE`). Unknown exchanges are written as the broker
  reports them, and a missing exchange is left blank, so new cases are easy to spot in the sheet.

## ⚠️ Manually maintained holdings

Some holdings can't be kept in sync automatically because of Google Finance issues (ticker or
exchange mismatches). List them in `~/.config/portfolio_pipeline/manual_holdings.json`, and the
script **never updates, appends or zeroes them**. Keep their Quantity up to date by hand in the
sheet.

The file lives outside the repo, like your credentials, because it reveals what you hold. Start
from the template:

```bash
cp config.example/manual_holdings.json ~/.config/portfolio_pipeline/
```

```json
[
  {"ticker": "XYZ",    "account": "WS - TFSA", "note": "sheet ticker"},
  {"ticker": "XYZ.UN", "account": "WS - TFSA", "note": "broker ticker"}
]
```

- `ticker` and `account` must match the sheet's Ticker and Account columns. `note` is for you.
- If the broker and the sheet use different tickers (e.g. `XYZ.UN` vs `XYZ`), list both.
- If the file doesn't exist, no holdings are treated as manual.
