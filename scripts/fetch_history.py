"""Download daily history to data/history/<SYM>_daily.csv via yfinance."""
import sys
import pandas as pd
import yfinance as yf

syms = sys.argv[1:] or ["SPY", "QQQ"]
for sym in syms:
    df = yf.download(sym, start="2000-01-01", auto_adjust=False, progress=False)
    df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]]
    df.index.name = "date"
    df.to_csv(f"data/history/{sym}_daily.csv")
    print(sym, len(df), df.index.min().date(), df.index.max().date())
