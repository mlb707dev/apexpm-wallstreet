"""Download recent intraday bars (yfinance allows ~60 days of 5m) to data/history/<SYM>_<N>m.csv."""
import sys
import pandas as pd
import yfinance as yf

syms = sys.argv[1:] or ["SPY"]
for sym in syms:
    for interval in ("5m", "15m"):
        df = yf.download(sym, period="60d", interval=interval, auto_adjust=False, progress=False, prepost=False)
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]]
        df.index.name = "ts"
        df.to_csv(f"data/history/{sym}_{interval}.csv")
        print(sym, interval, len(df), df.index.min(), df.index.max())
