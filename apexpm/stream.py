"""Fast layer: real-time prices and order updates over Alpaca websockets.

Two streams on one background thread:
  * market data (trades for the traded symbols): every print updates the
    in-memory price cache and triggers the exit engine for open positions in
    that symbol. Sub-second from print to order.
  * trade updates: fills, partial fills and cancels are pushed, so pending
    orders are promoted the moment they fill instead of on the next scan.

Feed: IEX (free, real-time but IEX-only prints) or SIP (full consolidated
tape; requires Alpaca's paid market-data subscription). Set DATA_FEED.

The slow loop still runs: entries, levels, calendar, review. If the stream
drops, alpaca-py reconnects; the loop keeps working on REST prices meanwhile.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

from .log import get_logger

log = get_logger()


class PriceCache:
    def __init__(self):
        self._p: Dict[str, Tuple[float, datetime]] = {}
        self._lock = threading.Lock()

    def put(self, symbol: str, price: float, ts: Optional[datetime] = None) -> None:
        with self._lock:
            self._p[symbol] = (price, ts or datetime.now(timezone.utc))

    def get(self, symbol: str, max_age_sec: Optional[float] = None) -> Optional[float]:
        with self._lock:
            v = self._p.get(symbol)
        if not v:
            return None
        price, ts = v
        if max_age_sec is not None and (datetime.now(timezone.utc) - ts).total_seconds() > max_age_sec:
            return None
        return price

    def age(self, symbol: str) -> Optional[float]:
        with self._lock:
            v = self._p.get(symbol)
        return None if not v else (datetime.now(timezone.utc) - v[1]).total_seconds()


class FastLayer:
    """Owns the websocket thread. on_tick(symbol, price) and on_order(event_dict) are
    called from the stream thread; keep them quick and thread-safe."""

    def __init__(self, cfg, symbols, on_tick: Callable[[str, float], None],
                 on_order: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self.symbols = list(symbols)
        self.on_tick = on_tick
        self.on_order = on_order
        self.prices = PriceCache()
        self.ticks = 0
        self.last_tick_at: Optional[datetime] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- lifecycle ----
    def start(self) -> None:
        if not (self.cfg.alpaca_api_key and self.cfg.alpaca_secret_key):
            log.warning("fast layer: no keys, not starting")
            return
        self._thread = threading.Thread(target=self._run, name="fast-layer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- internals ----
    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                asyncio.run(self._main())
                backoff = 1
            except Exception as exc:  # noqa: BLE001
                log.warning("fast layer stream error: %s; reconnecting in %ds", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _main(self) -> None:
        from alpaca.data.live import StockDataStream
        from alpaca.trading.stream import TradingStream

        feed = getattr(self.cfg, "data_feed", "iex")
        data = StockDataStream(self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key, feed=feed)

        async def on_trade(t):
            try:
                price = float(t.price)
                self.prices.put(t.symbol, price, getattr(t, "timestamp", None))
                self.ticks += 1
                self.last_tick_at = datetime.now(timezone.utc)
                self.on_tick(t.symbol, price)
            except Exception as exc:  # noqa: BLE001
                log.exception("on_tick failed: %s", exc)

        data.subscribe_trades(on_trade, *self.symbols)
        tasks = [asyncio.ensure_future(data._run_forever())]

        if self.on_order is not None:
            trading = TradingStream(self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key, paper=self.cfg.paper)

            async def on_update(u):
                try:
                    o = u.order
                    self.on_order({"event": str(u.event), "order_id": str(o.id), "symbol": o.symbol,
                                   "status": str(o.status.value if hasattr(o.status, "value") else o.status),
                                   "filled_qty": float(o.filled_qty or 0),
                                   "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None})
                except Exception as exc:  # noqa: BLE001
                    log.exception("on_order failed: %s", exc)

            trading.subscribe_trade_updates(on_update)
            tasks.append(asyncio.ensure_future(trading._run_forever()))

        log.info("fast layer: streaming %s on %s feed (%d streams)", self.symbols, feed, len(tasks))
        while not self._stop.is_set():
            await asyncio.sleep(1)
            for t in tasks:
                if t.done():
                    exc = t.exception() if not t.cancelled() else None
                    raise RuntimeError(f"stream task ended: {exc}")
        for t in tasks:
            t.cancel()
