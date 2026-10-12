"""
Investment data pipeline — main entry point.

Usage
-----
Run everything (Wealthsimple + Questrade):
    python pipeline.py

Run only Wealthsimple ingestion (no API credentials needed):
    python pipeline.py --ws-only

Run only Questrade extraction:
    python pipeline.py --qt-only

Limit Questrade activity history:
    python pipeline.py --start 2023-01-01 --end 2024-12-31
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


def move_ws_exports_to_raw():
    """
    Move any Wealthsimple CSVs sitting in the project root into data/raw/wealthsimple/.

    Wealthsimple downloads land wherever the browser saves files. This step
    keeps the project root clean and gives the extractor a predictable location.
    """
    root = Path(__file__).parent
    raw_ws = root / "data" / "raw" / "wealthsimple"
    raw_ws.mkdir(parents=True, exist_ok=True)

    moved = []
    for pattern in ("WS_activities-*.csv", "WS_holdings-*.csv"):
        for f in root.glob(pattern):
            dest = raw_ws / f.name
            f.rename(dest)
            moved.append(f"{f.name} → data/raw/wealthsimple/")

    if moved:
        print("[pipeline] Moved Wealthsimple exports to raw directory:")
        for m in moved:
            print(f"           {m}")


def run_wealthsimple():
    from extractors.wealthsimple import run
    print("\n── Wealthsimple ──────────────────────────────────────────")
    return run()


def run_questrade(start_date: str, end_date: str):
    from extractors.questrade import run
    print("\n── Questrade ─────────────────────────────────────────────")
    return run(start_date=start_date, end_date=end_date)


def main():
    parser = argparse.ArgumentParser(description="Investment data pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ws-only", action="store_true", help="Run only Wealthsimple ingestion")
    group.add_argument("--qt-only", action="store_true", help="Run only Questrade extraction")
    parser.add_argument("--start", default="2020-01-01", help="Activity history start date (YYYY-MM-DD)")
    parser.add_argument(
        "--end",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Activity history end date (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    print("=" * 58)
    print("  Investment Data Pipeline")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 58)

    # Housekeeping: move any WS exports from root to raw dir
    move_ws_exports_to_raw()

    results = {}

    if not args.qt_only:
        try:
            results["wealthsimple"] = run_wealthsimple()
        except FileNotFoundError as e:
            print(f"\n[wealthsimple] SKIPPED — {e}")

    if not args.ws_only:
        try:
            results["questrade"] = run_questrade(args.start, args.end)
        except EnvironmentError as e:
            print(f"\n[questrade] SKIPPED — {e}")
        except Exception as e:
            print(f"\n[questrade] ERROR — {e}")
            if "--qt-only" in sys.argv:
                raise

    print("\n── Done ──────────────────────────────────────────────────")
    processed = Path(__file__).parent / "data" / "processed"
    if processed.exists():
        for f in sorted(processed.glob("*.csv")):
            size_kb = f.stat().st_size / 1024
            print(f"  data/processed/{f.name}  ({size_kb:.1f} KB)")
    print()


if __name__ == "__main__":
    main()
