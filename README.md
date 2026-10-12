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
cp config.example/*.json ~/.config/portfolio_pipeline/
```

Then fill in each file:

| File | How to get it |
|---|---|
| `questrade_credentials.json` | Questrade > My Account > Security > Apps > Authorize a Personal App ([APIhub](https://login.questrade.com/APIhub)). Paste the refresh token. The pipeline rotates and saves new tokens automatically. |
| `google_credentials.json` | Google Cloud Console > create a service account > Keys > Add key > JSON. Replace the template with the downloaded file, and share your Google Sheet with the service account's `client_email` (Editor). |
| `sheets_config.json` | The sheet ID from your Google Sheet URL: `docs.google.com/spreadsheets/d/<spreadsheet_id>/edit` |

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
.venv/bin/python portfolio_updater.py     # Push to the Portfolio sheet
.venv/bin/python etf_sectors.py           # Refresh ETF sector weightings
```

Raw and processed data (`data/`) are git-ignored because they contain personal financial data.
