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
