"""
setup_sheet.py

Builds the 'Portfolio' and 'Parameters' tabs in your Google Sheet, ready for
portfolio_updater.py: header, column formats and widths, a 'Portfolio' table
(so formulas like SUM(Portfolio[CAD Market Value]) work), volatility colour
scales, and the Parameters inputs the formulas use. The layout is defined in
sheet_template.py.

Run it once, on a blank spreadsheet shared with the service account:
    .venv/bin/python setup_sheet.py

It never overwrites anything: if either tab already exists, it stops.
Then run pipeline.py and portfolio_updater.py; each holding is appended as a
row with its formulas filled in.
"""

import json
import sys

from portfolio_updater import GOOGLE_CREDS, SHEETS_API_SCOPE, SHEETS_CONFIG
from sheet_template import (
    BAND_COLOR, COLUMNS, GRADIENT_MAX, HEADER_COLOR, PARAMETERS, PARAMETERS_TAB,
    PORTFOLIO_TAB, TABLE_NAME, localize,
)


def build_requests(portfolio_id: int, parameters_id: int) -> list:
    """batchUpdate requests that format the Portfolio tab and create its table."""
    ncols    = len(COLUMNS)
    requests = []

    # Table: header only; portfolio_updater.py appends rows into it, which extends
    # the table, and applies the number formats to those rows (a table cell's
    # format otherwise comes from its column type, without the patterns).
    requests.append({"addTable": {"table": {
        "name": TABLE_NAME,
        "range": {"sheetId": portfolio_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": ncols},
        "rowsProperties": {
            "headerColorStyle":     {"rgbColor": HEADER_COLOR},
            "firstBandColorStyle":  {"rgbColor": {"red": 1, "green": 1, "blue": 1}},
            "secondBandColorStyle": {"rgbColor": BAND_COLOR},
        },
        "columnProperties": [
            {"columnIndex": i, "columnName": c["name"]} for i, c in enumerate(COLUMNS)
        ],
    }}})

    for i, c in enumerate(COLUMNS):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": portfolio_id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": c["width"]}, "fields": "pixelSize",
        }})
        if c.get("gradient"):
            requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [{"sheetId": portfolio_id, "startRowIndex": 1,
                            "startColumnIndex": i, "endColumnIndex": i + 1}],
                "gradientRule": {
                    "minpoint": {"type": "MIN", "colorStyle": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}},
                    "maxpoint": {"type": "MAX", "colorStyle": {"rgbColor": GRADIENT_MAX}},
                },
            }}})

    requests.append({"repeatCell": {
        "range": {"sheetId": portfolio_id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }})
    requests.append({"repeatCell": {
        "range": {"sheetId": parameters_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold",
    }})
    requests.append({"repeatCell": {
        "range": {"sheetId": parameters_id, "startRowIndex": 2, "endRowIndex": 3,
                  "startColumnIndex": 1, "endColumnIndex": 2},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
        "fields": "userEnteredFormat.numberFormat",
    }})
    return requests


def main():
    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials

    with open(SHEETS_CONFIG) as f:
        spreadsheet_id = json.load(f)["spreadsheet_id"].strip()
    creds   = Credentials.from_service_account_file(str(GOOGLE_CREDS), scopes=SHEETS_API_SCOPE)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    meta   = service.get(spreadsheetId=spreadsheet_id,
                         fields="properties(title,locale),sheets(properties(title))").execute()
    locale = meta["properties"].get("locale", "en_US")
    titles = [s["properties"]["title"] for s in meta["sheets"]]
    print(f"[setup] Spreadsheet '{meta['properties']['title']}' (locale {locale})")

    existing = [t for t in (PORTFOLIO_TAB, PARAMETERS_TAB) if t in titles]
    if existing:
        sys.exit(f"[setup] Stopped: tab(s) {existing} already exist. setup_sheet.py only builds a "
                 "blank spreadsheet and never overwrites; rename or delete those tabs to start over.")

    # ── Tabs ─────────────────────────────────────────────────────────────────
    reply = service.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [
        {"addSheet": {"properties": {"title": PORTFOLIO_TAB, "index": 0, "gridProperties": {
            "rowCount": 1000, "columnCount": len(COLUMNS),
            "frozenRowCount": 1, "frozenColumnCount": 3}}}},
        {"addSheet": {"properties": {"title": PARAMETERS_TAB, "index": 1}}},
    ]}).execute()
    portfolio_id  = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
    parameters_id = reply["replies"][1]["addSheet"]["properties"]["sheetId"]
    print(f"[setup] Added tabs '{PORTFOLIO_TAB}' and '{PARAMETERS_TAB}'")

    # ── Values: Parameters, then the Portfolio header ────────────────────────
    service.values().batchUpdate(spreadsheetId=spreadsheet_id, body={
        "valueInputOption": "USER_ENTERED",
        "data": [
            *[{"range": f"'{PARAMETERS_TAB}'!{cell}", "values": [[localize(v, locale) if isinstance(v, str) else v]]}
              for cell, v in PARAMETERS.items()],
            {"range": f"'{PORTFOLIO_TAB}'!A1", "values": [[c["name"] for c in COLUMNS]]},
        ],
    }).execute()

    # ── Table, formats, widths, colour scales ────────────────────────────────
    service.batchUpdate(spreadsheetId=spreadsheet_id,
                        body={"requests": build_requests(portfolio_id, parameters_id)}).execute()
    print(f"[setup] Created table '{TABLE_NAME}' with {len(COLUMNS)} columns, formats and colour scales")

    print("\n[setup] Done. Next:\n"
          "  .venv/bin/python pipeline.py\n"
          "  .venv/bin/python portfolio_updater.py\n"
          "Then fill in the manual columns (ETF, Liquidity, Asset Class, Region, Tags, Notes).")


if __name__ == "__main__":
    main()
