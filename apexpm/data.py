"""Market data from Alpaca. Daily bars for levels, intraday for ribbon."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd


class DataFeed:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self._crypto = None
        if cfg.alpaca_api_key and cfg.alpaca_secret_key:
            from alpaca.data.historical import StockHistoricalDataClient
            self._client = StockHistoricalDataClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
            try:
                from alpaca.data.historical import CryptoHistoricalDataClient
                self._crypto = CryptoHistoricalDataClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)
            except Exception:  # noqa: BLE001
                self._crypto = None

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return "/" in symbol

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _bars(self, symbol: str, timeframe, start: datetime, end: Optional[datetime] = None) -> pd.DataFrame:
        if self.is_crypto(symbol):
            from alpaca.data.requests import CryptoBarsRequest
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end)
            bars = self._crypto.get_crypto_bars(req).df
        else:
            from alpaca.data.requests import StockBarsRequest
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end,
                                   feed="iex")
            bars = self._client.get_stock_bars(req).df
        if bars.empty:
            return bars
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")
        return bars[["open", "high", "low", "close", "volume"]].copy()

    def daily(self, symbol: str, lookback_days: int = 60) -> pd.DataFrame:
        """Completed daily bars only (excludes today's partial bar)."""
        from alpaca.data.timeframe import TimeFrame
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        df = self._bars(symbol, TimeFrame.Day, start)
        if df.empty:
            return df
        today = now.date()
        return df[df.index.date < today]

    def intraday(self, symbol: str, minutes: int, lookback_bars: int = 120) -> pd.DataFrame:
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        tf = TimeFrame(minutes, TimeFrameUnit.Minute)
        now = datetime.now(timezone.utc)
        # generous window: lookback bars across up to 5 sessions
        start = now - timedelta(days=7)
        df = self._bars(symbol, tf, start)
        return df.tail(lookback_bars)

    def last_trade(self, symbol: str):
        """(price, timestamp) of the latest trade, or (None, None)."""
        if self.is_crypto(symbol):
            from alpaca.data.requests import CryptoLatestTradeRequest
            t = self._crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=symbol))
        else:
            from alpaca.data.requests import StockLatestTradeRequest
            t = self._client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed="iex"))
        trade = t.get(symbol)
        if not trade:
            return None, None
        return float(trade.price), trade.timestamp

    def last_price(self, symbol: str, max_age_sec: Optional[int] = None) -> Optional[float]:
        """Latest trade price; None if missing or older than max_age_sec."""
        price, ts = self.last_trade(symbol)
        if price is None:
            return None
        if max_age_sec and ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ts).total_seconds() > max_age_sec:
                return None
        return price

    # ---- extended hours (overnight desk) -------------------------------------------------
    def ext_bars(self, symbol: str, minutes: int = 5, days: int = 30, feed: str = "iex",
                 overnight_feed: str = "boats", start: Optional[datetime] = None, end: Optional[datetime] = None) -> pd.DataFrame:
        """All-session bars for the overnight desk, tagged with a `session` column like the history files:
        `feed` for pre/rth/after-hours and the BOATS feed for 20:00-04:00. On the free plan BOATS bars are
        15 minutes delayed and the IEX tape is thin outside regular hours (research/overnight/thesis.md)."""
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from .ext_sessions import session_of_index
        start = start or (datetime.now(timezone.utc) - timedelta(days=days))
        parts = []
        for fd, keep_overnight in ((feed, False), (overnight_feed, True)):
            try:
                req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame(minutes, TimeFrameUnit.Minute), start=start, end=end, feed=fd,
                                       adjustment="split")
                b = self._client.get_stock_bars(req).df
            except Exception:  # noqa: BLE001
                continue
            if b is None or b.empty:
                continue
            if isinstance(b.index, pd.MultiIndex):
                b = b.xs(symbol, level="symbol")
            b = b[["open", "high", "low", "close", "volume"]].copy()
            b["session"] = session_of_index(b.index)
            b = b[(b["session"] == "overnight") if keep_overnight else (b["session"] != "overnight")]
            parts.append(b)
        if not parts:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "session"])
        out = pd.concat(parts).sort_index()
        return out[~out.index.duplicated(keep="first")]

    def ext_quote(self, symbol: str, session: str, feed: str = "iex"):
        """(bid, ask) for the desk's spread check and exit pricing; the overnight session uses Alpaca's
        'overnight' latest-quote feed (indicative on the free plan). (None, None) when unavailable."""
        from alpaca.data.requests import StockLatestQuoteRequest
        fd = "overnight" if session == "overnight" else feed
        try:
            q = self._client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=fd)).get(symbol)
        except Exception:  # noqa: BLE001
            return None, None
        if not q or not q.bid_price or not q.ask_price or q.ask_price < q.bid_price:
            return None, None
        return float(q.bid_price), float(q.ask_price)

