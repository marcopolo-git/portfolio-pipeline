# portfolio-pipeline

Pulls holdings from Wealthsimple (CSV exports) and Questrade (REST API),
normalises them into CSVs, and syncs them to a Google Sheet.

| Script | What it does |
|---|---|
| `pipeline.py` | Ingests Wealthsimple CSVs and Questrade API data into `data/processed/` |
| `setup_sheet.py` | Builds the `Portfolio` and `Parameters` tabs in a blank Google Sheet (**run once**) |
| `portfolio_updater.py` | Updates the `Portfolio` sheet with the latest quantities and adds new holdings |
| `etf_sectors.py` | Fetches ETF sector weightings (yfinance) into an `ETF Sectors` sheet |

The sheet's columns, formats and formulas are defined in [`sheet_template.py`](sheet_template.py).

## At a glance

| When | What to run |
|---|---|
| **Once**, when setting up | Steps 1–4 of [One-time setup](#one-time-setup), including `setup_sheet.py` |
| **Every time** you update your portfolio | Export from Wealthsimple, then `pipeline.py` and `portfolio_updater.py` (see [Updating your portfolio](#updating-your-portfolio)) |

## One-time setup

Do these steps once. You never need to repeat them to update your portfolio.

### 1. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Credentials

Credentials live outside the repo, in `~/.config/portfolio_pipeline/`. Copy the templates from
[`config.example/`](config.example/):

```bash
mkdir -p ~/.config/portfolio_pipeline
cp -n config.example/*.json ~/.config/portfolio_pipeline/
```

`-n` skips files that already exist, so re-running it never overwrites your real credentials.
Then fill in:

| File | How to get it |
|---|---|
| `questrade_credentials.json` | Questrade > My Account > Security > Apps > Authorize a Personal App ([API hub](https://apphub.questrade.com/UI/UserApps.aspx)). Paste the refresh token. The pipeline rotates and saves new tokens automatically. |
| `google_credentials.json` | Google Cloud Console > create a project, enable the **Google Sheets API**, create a service account > Keys > Add key > JSON. Replace the template with the downloaded file. |
| `manual_holdings.json` | Optional. Holdings you maintain by hand; see [Manually maintained holdings](#manually-maintained-holdings). Replace or empty (`[]`) the placeholder entries. |

`sheets_config.json` is filled in during the next step.

### 3. Create the Google Sheet

1. In Google Sheets, create a **blank** spreadsheet.
2. Click **Share** and add the service account's `client_email` (found in
   `google_credentials.json`) as **Editor**. The pipeline can only write to sheets shared with it.
3. Copy the sheet ID from its URL, `docs.google.com/spreadsheets/d/<spreadsheet_id>/edit`, into
   `~/.config/portfolio_pipeline/sheets_config.json`:

   ```json
   {"spreadsheet_id": "<spreadsheet_id>"}
   ```

### 4. Build the Portfolio worksheet (once)

```bash
.venv/bin/python setup_sheet.py
```

This creates two tabs in the blank sheet:

- **Portfolio**: a `Portfolio` table with all columns, number formats and volatility colour
  scales. It starts empty; your holdings are added the first time you update.
- **Parameters**: the history window (start date, number of days) used by the `Param.` columns.

Run it **only once**. It stops without changing anything if either tab already exists, so it
can't overwrite a sheet you already use. If you already have a Portfolio sheet built for this
pipeline, skip this step and put that sheet's ID in `sheets_config.json` instead.

## Updating your portfolio

Run these steps whenever you want fresh numbers in the sheet.

### 1. Export from Wealthsimple

Wealthsimple has no public API. Export a fresh holdings report from the web app each time:
Portfolio > Holdings report.

Save it in `data/raw/wealthsimple/`, and only there: that folder is git-ignored, so your
exports can never be committed by accident. Don't drop it in the project root. The newest
export is used; older ones can stay.

Questrade needs no export: its data is fetched from the API.

### 2. Run the pipeline, then the updater

```bash
.venv/bin/python pipeline.py              # fetch and normalise Wealthsimple + Questrade data
.venv/bin/python portfolio_updater.py     # push it to the Portfolio sheet
```

`portfolio_updater.py` updates quantities, adds new holdings with their formulas filled in,
and sets fully sold holdings to 0 (see [How the sheet update works](#how-the-sheet-update-works)).

**After the first update** (and whenever new holdings appear), fill in the manual columns for
the new rows: ETF, Liquidity, Asset Class, Region, Tags, Notes. Formulas adapt to the sheet's
locale, e.g. `;` separators for French.

### 3. Optional: ETF sector weightings

```bash
.venv/bin/python etf_sectors.py           # rows with a value in the ETF column
.venv/bin/python etf_sectors.py QQQ XEQT  # ...or only the tickers given
```

Writes an `ETF Sectors` tab; it's created on the first run.

### Pipeline options

```bash
.venv/bin/python pipeline.py --ws-only    # Wealthsimple only (no Questrade credentials needed)
.venv/bin/python pipeline.py --qt-only    # Questrade only
```

Raw and processed data (`data/`) are git-ignored because they contain personal financial data.

## How the sheet update works

For existing rows, `portfolio_updater.py` only writes the **Import Date** and **Quantity** columns of Wealthsimple
(`WS - …`) and Questrade (`QT - …`) rows. Everything else in the sheet is left alone.

- **Existing holdings** are matched by ticker, account and exchange, and updated. A blank
  exchange on either side matches any exchange.
- **New holdings** are appended at the bottom with their formula columns filled in (from
  `sheet_template.py`) and the number formats of the row above. Manual columns are left blank
  for you to fill in.
- **Sold holdings** (no longer reported by the broker) get Quantity `0`. The row is kept, not
  deleted. This only happens for accounts the broker reported in this run, and only when the
  row's Import Date is readable and not newer than that data, so stale or failed exports never
  zero a holding.
- **Cash** is matched to rows with a blank Ticker by account and currency.
- **Exchanges** come from the Wealthsimple export and from Questrade's listing exchange (e.g.
  Questrade `ARCA` → `NYSEARCA`, `TSX` → `TSE`). Unknown exchanges are written as the broker
  reports them, and a missing exchange is left blank, so new cases are easy to spot in the sheet.

## Manually maintained holdings

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
