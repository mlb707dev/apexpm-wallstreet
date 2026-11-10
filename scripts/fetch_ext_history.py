"""Download extended-hours 5m bars to data/history/<SYM>_5m_ext_alpaca.csv (the overnight desk's history).

Usage: .venv/bin/python scripts/fetch_ext_history.py [SYM ...] [--start 2021-01-01]

Kept apart from the regular-hours files (<SYM>_5m_alpaca.csv) the existing backtests read, which stay
untouched. Two sources, measured 2026-11-07 (research/overnight/thesis.md):
  * SIP (consolidated tape) for pre-market 04:00-09:30, regular hours and after-hours 16:00-20:00. The free
    plan serves SIP history as long as the request ends >= 15 minutes ago. The IEX feed the live loop uses
    is nearly empty outside regular hours (SPY: ~6 pre-market bars a day), so it cannot be the history.
  * BOATS (Blue Ocean ATS) for the overnight session 20:00-04:00. Alpaca's history starts 2024-09-16.
Regular-hours SIP bars are kept in the file too (session "rth"): the overnight strategies need the
regular-session close, high and low from the same tape, and a pre-market trade may exit into the open.
Prices are split-adjusted (NVDA, TSLA and AMZN split inside the window; a raw split would read as a huge gap
and poison the ATR for weeks). Columns: ts (UTC), open, high, low, close, volume, trade_count, vwap, session (pre/rth/ah/overnight), feed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apexpm.config import Config  # noqa: E402
from apexpm.ext_sessions import session_of_index  # noqa: E402

BOATS_START = "2024-09-16"
DEFAULT_SYMBOLS = ["SPY", "QQQ", "IWM", "NVDA", "TSLA", "AAPL", "AMD", "AMZN", "META", "MSFT"]


def _bars(client, sym: str, start: datetime, end: datetime, feed: str) -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame(5, TimeFrameUnit.Minute), start=start, end=end,
                           feed=feed, adjustment="split")
    df = client.get_stock_bars(req).df
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(sym, level="symbol")
    cols = [c for c in ("open", "high", "low", "close", "volume", "trade_count", "vwap") if c in df]
    return df[cols].copy()


def build(sip: pd.DataFrame, boats: pd.DataFrame) -> pd.DataFrame:
    """Merge the two feeds: SIP for pre/rth/ah, BOATS only for the overnight window (never overlapping)."""
    parts = []
    if len(sip):
        s = sip.copy()
        s["session"] = session_of_index(s.index)
        s = s[s["session"] != "overnight"]
        s["feed"] = "sip"
        parts.append(s)
    if len(boats):
        b = boats.copy()
        b["session"] = session_of_index(b.index)
        b = b[b["session"] == "overnight"]
        b["feed"] = "boats"
        parts.append(b)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    out.index.name = "ts"
    return out


def main(symbols, start: str) -> None:
    from alpaca.data.historical import StockHistoricalDataClient
    cfg = Config()
    client = StockHistoricalDataClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.now(timezone.utc) - timedelta(minutes=20)      # free plan: SIP history must end >= 15 min ago
    os.makedirs("data/history", exist_ok=True)
    for sym in symbols:
        sip = _bars(client, sym, start_dt, end_dt, "sip")
        boats_start = max(start_dt, datetime.fromisoformat(BOATS_START).replace(tzinfo=timezone.utc))
        try:
            boats = _bars(client, sym, boats_start, end_dt, "boats")
        except Exception as exc:  # noqa: BLE001
            print(sym, "boats unavailable:", exc)
            boats = pd.DataFrame()
        df = build(sip, boats)
        out = f"data/history/{sym}_5m_ext_alpaca.csv"
        df.to_csv(out)
        counts = df["session"].value_counts().to_dict() if len(df) else {}
        print(sym, len(df), counts, df.index.min() if len(df) else None, df.index.max() if len(df) else None, "->", out, flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    start = "2021-01-01"
    if "--start" in args:
        i = args.index("--start")
        start = args[i + 1]
        del args[i:i + 2]
    main([a.upper() for a in args] or DEFAULT_SYMBOLS, start)
