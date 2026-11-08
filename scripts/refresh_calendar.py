"""Refresh earnings dates for the market-mover watchlist into the journal.
Usage: .venv/bin/python scripts/refresh_calendar.py [DB_PATH]"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apexpm.calendar import MARKET_MOVERS, ensure_table  # noqa: E402
from apexpm.config import Config  # noqa: E402
from apexpm.db import Journal  # noqa: E402


def main(db_path: str) -> None:
    import pandas as pd
    import yfinance as yf
    j = Journal(db_path)
    ensure_table(j)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for sym in sorted(set(MARKET_MOVERS + Config().symbols)):
        try:
            df = yf.Ticker(sym).get_earnings_dates(limit=8)
        except Exception as exc:  # noqa: BLE001
            print(sym, "skip:", type(exc).__name__)
            continue
        if df is None or len(df) == 0:
            continue
        for ts, _row in df.iterrows():
            d = pd.Timestamp(ts).tz_convert("America/New_York") if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts)
            if d.date() < datetime.now().date():
                continue
            timing = "amc" if d.hour >= 15 else "bmo" if d.hour and d.hour < 9 else "tbd"
            with j.tx() as c:
                c.execute("INSERT OR REPLACE INTO calendar_events (kind, symbol, date, timing, source, fetched_at) "
                          "VALUES ('earnings', ?, ?, ?, 'yfinance', ?)", (sym, d.date().isoformat(), timing, now))
            n += 1
    print("earnings rows upserted:", n)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Config().db_path)
