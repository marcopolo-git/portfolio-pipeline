"""
sheet_template.py

Definition of the Google Sheet the pipeline writes to: the 'Portfolio' table
(columns, formats and per-row formulas) and the 'Parameters' tab its formulas use.

Used by:
  setup_sheet.py         builds both tabs in a blank spreadsheet
  portfolio_updater.py   fills the formula columns of holdings it appends

Formulas are written in en_US syntax with {Column Name} placeholders, which
row_formulas() turns into same-row cell references (e.g. {Price} → F12). That
keeps them correct whatever order the columns are in. {SYMBOL} is the
Google Finance symbol: EXCHANGE:TICKER, or just TICKER when Exchange is blank.

The 'Param.' columns measure history over the window set in the 'Parameters'
tab (B3 = start date, B4 = number of days). If a spreadsheet has no Parameters
tab, they fall back to a fixed window of DEFAULT_WINDOW_DAYS ending today.
"""

import re

PORTFOLIO_TAB       = "Portfolio"
PARAMETERS_TAB      = "Parameters"   # optional; see module docstring
TABLE_NAME          = "Portfolio"    # structured references use it: Portfolio[CAD Market Value]
BASE_CURRENCY       = "CAD"
DEFAULT_WINDOW_DAYS = 700

# Parameters tab built by setup_sheet.py (A1 notation → value). The Portfolio
# formulas read B3 (start date) and B4 (number of days), so keep these addresses.
PARAMETERS = {
    "A1": "Parameters",
    "A3": "Start Date",       "B3": "=TODAY()-B4",
    "A4": "Number of Days",   "B4": DEFAULT_WINDOW_DAYS,
    "A6": "Used by the Portfolio columns ending in 'Param.': history runs from Start Date for Number of Days.",
}

# {START} / {DAYS} in formulas: the 'Param.' history window
_WINDOW = {
    True:  {"START": f"{PARAMETERS_TAB}!$B$3", "DAYS": f"{PARAMETERS_TAB}!$B$4"},
    False: {"START": f"(TODAY()-{DEFAULT_WINDOW_DAYS})", "DAYS": str(DEFAULT_WINDOW_DAYS)},
}

SYMBOL = 'IF({Exchange}="",{Ticker},{Exchange}&":"&{Ticker})'

# Weekly return series between two dates, used by both volatility columns.
_WEEKLY_RETURNS = (
    '(INDEX(GOOGLEFINANCE({SYMBOL},"close",{start}+7,{end},"weekly"),0,2)'
    ' / INDEX(GOOGLEFINANCE({SYMBOL},"close",{start},{end_minus_7},"weekly"),0,2)) - 1'
)


def _weekly_sd(start: str, end: str, end_minus_7: str) -> str:
    returns = _WEEKLY_RETURNS.format(SYMBOL="{SYMBOL}", start=start, end=end, end_minus_7=end_minus_7)
    return f"=STDEV(ARRAYFORMULA(FILTER({returns},ISNUMBER({returns}))))"


# Portfolio columns, in order.
#   source:  pipeline — written by portfolio_updater.py
#            formula  — filled in by the formulas below
#            manual   — you fill it in
#   formula: for holdings;  cash: for cash rows (blank Ticker). None → left blank.
#   type / pattern: Sheets number format. Table columns are left untyped: the API
#   ignores number-format patterns on cells of typed table columns.
COLUMNS = [
    {"name": "Exchange",    "source": "pipeline", "width": 77},
    {"name": "Ticker",      "source": "pipeline", "width": 72},
    {"name": "Name",        "source": "formula",  "width": 290,
     "formula": '=GOOGLEFINANCE({SYMBOL},"name")', "cash": "Cash"},
    {"name": "Import Date", "source": "pipeline", "width": 110, "type": "DATE", "pattern": "yyyy-mm-dd"},
    {"name": "Quantity",    "source": "pipeline", "width": 100, "type": "NUMBER", "pattern": "#,##0.00"},
    {"name": "Price",       "source": "formula",  "width": 100, "type": "NUMBER", "pattern": "#,##0.00;(#,##0.00)",
     "formula": "=GOOGLEFINANCE({SYMBOL})", "cash": 1},
    {"name": "Market Value", "source": "formula", "width": 120, "type": "NUMBER", "pattern": "#,##0",
     "formula": "={Quantity}*{Price}", "cash": "={Quantity}*{Price}"},
    {"name": "CAD Market Value", "source": "formula", "width": 140, "type": "CURRENCY", "pattern": "#,##0;(#,##0)",
     "formula": f'=IF({{Currency}}="{BASE_CURRENCY}",{{Market Value}},'
                f'{{Market Value}}*GOOGLEFINANCE("CURRENCY:"&{{Currency}}&"{BASE_CURRENCY}"))'},
    {"name": "Currency",    "source": "pipeline", "width": 80},
    {"name": "Account",     "source": "pipeline", "width": 110},
    {"name": "ETF",         "source": "manual",   "width": 60},   # any value marks an ETF for etf_sectors.py
    {"name": "Liquidity",   "source": "manual",   "width": 100},
    {"name": "Asset Class", "source": "manual",   "width": 110},
    {"name": "Region",      "source": "manual",   "width": 105},
    {"name": "Tags",        "source": "manual",   "width": 105},
    {"name": "Notes",       "source": "manual",   "width": 160, "type": "TEXT"},
    {"name": "Portfolio Weight %", "source": "formula", "width": 120,
     "type": "PERCENT", "pattern": "0.00%",
     "formula": f"={{CAD Market Value}}/SUM({TABLE_NAME}[CAD Market Value])"},
    {"name": "Weekly SD - 52 weeks", "source": "formula", "width": 150, "type": "PERCENT", "pattern": "0.0%", "gradient": True,
     "formula": _weekly_sd("TODAY()-371", "TODAY()", "TODAY()-7"), "cash": None},
    {"name": "Weekly SD - Param.", "source": "formula", "width": 140, "type": "PERCENT", "pattern": "0.0%",
     "formula": _weekly_sd("{START}", "{DAYS}", "{DAYS}-7"), "cash": None},
    {"name": "Yearly SD - Param.", "source": "formula", "width": 140, "type": "PERCENT", "pattern": "0.0%", "gradient": True,
     "formula": "={Weekly SD - Param.}*SQRT(52)", "cash": None},
    {"name": "Beta",        "source": "formula",  "width": 80, "type": "NUMBER", "pattern": "0.0",
     "formula": '=IFERROR(GOOGLEFINANCE({SYMBOL},"beta"),1)', "cash": None},
    {"name": "Drawdown - 52 weeks", "source": "formula", "width": 150, "type": "PERCENT", "pattern": "0%",
     "formula": '=IFERROR(GOOGLEFINANCE({SYMBOL},"low52")/{Price}-1,"")', "cash": None},
    {"name": "Drawdown - Param.", "source": "formula", "width": 140, "type": "PERCENT", "pattern": "0%",
     "formula": '=IFERROR(MIN(INDEX(GOOGLEFINANCE({SYMBOL},"low",{START},{DAYS}),0,2))'
                '/{Price}-1,"")', "cash": None},
]

HEADER_COLOR = {"red": 0.208, "green": 0.408, "blue": 0.329}   # dark green
BAND_COLOR   = {"red": 0.965, "green": 0.973, "blue": 0.976}   # light grey
GRADIENT_MAX = {"red": 0.902, "green": 0.486, "blue": 0.451}   # red for high volatility


def _col_letter(n: int) -> str:
    """1-based column number → A1 letter (1→A, 27→AA)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# Locales that write numbers with a decimal comma use ';' between formula arguments.
_DECIMAL_COMMA_LANGS = {
    "bg", "ca", "cs", "da", "de", "el", "es", "et", "fi", "fr", "hr", "hu", "id", "it",
    "lt", "lv", "nb", "nl", "nn", "no", "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv",
    "tr", "uk", "vi",
}
_DECIMAL_POINT_EXCEPTIONS = {"de_CH", "es_MX", "es_US", "es_PR", "es_DO", "es_GT", "es_HN",
                             "es_NI", "es_PA", "es_SV", "it_CH"}


def uses_semicolons(locale: str) -> bool:
    """True if a spreadsheet with this locale (e.g. 'fr_FR') expects ';' between arguments."""
    locale = locale or "en_US"
    return locale.split("_")[0] in _DECIMAL_COMMA_LANGS and locale not in _DECIMAL_POINT_EXCEPTIONS


def localize(formula: str, locale: str) -> str:
    """Convert an en_US formula to the spreadsheet's syntax (',' → ';', 1.5 → 1,5 outside strings)."""
    if not uses_semicolons(locale) or not formula.startswith("="):
        return formula
    parts = formula.split('"')
    for i in range(0, len(parts), 2):                 # even parts are outside string literals
        p = re.sub(r"(?<=\d)\.(?=\d)", ",", parts[i].replace(",", ";"))
        parts[i] = p
    return '"'.join(parts)


def number_format_requests(sheet_id: int, cols: dict, start_row: int, end_row: int) -> list:
    """
    repeatCell requests applying the template's number formats to sheet rows
    start_row..end_row (1-based, inclusive). cols maps column names to 1-based
    positions; columns the sheet doesn't have are skipped.
    """
    requests = []
    for col in COLUMNS:
        if "type" not in col or col["name"] not in cols:
            continue
        i = cols[col["name"]] - 1
        fmt = {"type": col["type"], **({"pattern": col["pattern"]} if "pattern" in col else {})}
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": start_row - 1, "endRowIndex": end_row,
                      "startColumnIndex": i, "endColumnIndex": i + 1},
            "cell": {"userEnteredFormat": {"numberFormat": fmt}},
            "fields": "userEnteredFormat.numberFormat",
        }})
    return requests


def row_formulas(row: int, cols: dict, cash: bool, locale: str,
                 has_parameters: bool = False) -> dict:
    """
    Formula column values for one Portfolio row, as {column name: value}.

    cols maps column names to 1-based positions in the sheet; formula columns
    missing from the sheet, or referring to a missing column, are skipped.
    has_parameters: the spreadsheet has a Parameters tab to read the 'Param.' window from.
    """
    def ref(match):
        name = match.group(1)
        if name not in cols:
            raise KeyError(name)
        return f"{_col_letter(cols[name])}{row}"

    out = {}
    for col in COLUMNS:
        if col["source"] != "formula" or col["name"] not in cols:
            continue
        value = col.get("cash", col.get("formula")) if cash else col.get("formula")
        if value is None:
            continue
        if isinstance(value, str):
            value = value.replace("{SYMBOL}", SYMBOL)
            for key, sub in _WINDOW[has_parameters].items():
                value = value.replace("{" + key + "}", sub)
            try:
                value = re.sub(r"\{([^{}]+)\}", ref, value)
            except KeyError:
                continue
            value = localize(value, locale)
        out[col["name"]] = value
    return out
