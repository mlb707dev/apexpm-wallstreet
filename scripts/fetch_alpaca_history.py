"""Download multi-year intraday bars from Alpaca to data/history/<SYM>_<tf>_alpaca.csv.

Usage: .venv/bin/python scripts/fetch_alpaca_history.py SPY 2021-01-01
Uses the IEX feed (free tier). Writes 5m and 15m RTH-only bars in UTC.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apexpm.config import Config  # noqa: E402


def main(sym: str, start: str) -> None:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    cfg = Config()
    client = StockHistoricalDataClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    os.makedirs("data/history", exist_ok=True)
    for minutes in (5, 15):
        req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
                               start=start_dt, feed="iex", adjustment="raw")
        df = client.get_stock_bars(req).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        df = df[["open", "high", "low", "close", "volume"]].copy()
        et = df.index.tz_convert("America/New_York")
        mins = et.hour * 60 + et.minute
        if minutes == 5:
            # pre-market high/low per day (04:00-09:29 ET), kept apart so the backtester sees the same
            # pre-market levels the live loop does, while every indicator stays on regular-hours bars
            pm = df[(mins >= 4 * 60) & (mins < 9 * 60 + 30)]
            if len(pm):
                pdays = pm.index.tz_convert("America/New_York").date
                agg = pd.DataFrame({"pm_high": pm["high"].groupby(pdays).max(), "pm_low": pm["low"].groupby(pdays).min()})
                agg.index.name = "date"
                agg.to_csv(f"data/history/{sym}_pm_alpaca.csv")
                print(sym, "pre-market days", len(agg))
        # regular hours only (09:30–16:00 ET), to match the live loop's indicator bars
        df = df[(mins >= 9 * 60 + 30) & (mins < 16 * 60)]
        df.index.name = "ts"
        out = f"data/history/{sym}_{minutes}m_alpaca.csv"
        df.to_csv(out)
        print(sym, f"{minutes}m", len(df), df.index.min(), df.index.max(), "->", out)
    # daily from Alpaca too, so the whole study is one source
    req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=start_dt, feed="iex", adjustment="raw")
    d = client.get_stock_bars(req).df
    if isinstance(d.index, pd.MultiIndex):
        d = d.xs(sym, level="symbol")
    d = d[["open", "high", "low", "close", "volume"]].copy()
    d.index = d.index.tz_convert("America/New_York").normalize().tz_localize(None)
    d.index.name = "date"
    d.to_csv(f"data/history/{sym}_daily_alpaca.csv")
    print(sym, "daily", len(d), d.index.min().date(), d.index.max().date())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "SPY", sys.argv[2] if len(sys.argv) > 2 else "2021-01-01")
