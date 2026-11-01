"""Alpaca wrapper. Paper by default. DRY_RUN never sends an order.

Every method that touches the broker is explicit about what it returns when
offline (no keys) so the loop can reason about it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .log import get_logger

log = get_logger()


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    side: str  # long | short


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float


@dataclass
class OrderState:
    id: str
    status: str            # new | partially_filled | filled | canceled | expired | rejected | ...
    filled_qty: float
    filled_avg_price: Optional[float]

    @property
    def done(self) -> bool:
        return self.status in ("filled", "canceled", "expired", "rejected", "replaced")


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        if cfg.alpaca_api_key and cfg.alpaca_secret_key:
            from alpaca.trading.client import TradingClient
            self._client = TradingClient(cfg.alpaca_api_key, cfg.alpaca_secret_key,
                                         paper=cfg.paper)

    @property
    def connected(self) -> bool:
        return self._client is not None

    # ---- reads ------------------------------------------------------------
    def account(self) -> Account:
        if not self._client:
            return Account(equity=100_000.0, cash=100_000.0, buying_power=100_000.0)
        a = self._client.get_account()
        return Account(equity=float(a.equity), cash=float(a.cash),
                       buying_power=float(a.buying_power))

    def positions(self) -> List[Position]:
        if not self._client:
            return []
        out = []
        for p in self._client.get_all_positions():
            out.append(Position(symbol=p.symbol, qty=abs(float(p.qty)),
                                avg_entry=float(p.avg_entry_price),
                                side="long" if float(p.qty) > 0 else "short"))
        return out

    def position_map(self) -> Dict[str, Position]:
        return {p.symbol: p for p in self.positions()}

    def order_state(self, order_id: str) -> Optional[OrderState]:
        if not self._client or not order_id:
            return None
        o = self._client.get_order_by_id(order_id)
        return OrderState(id=str(o.id), status=str(o.status.value if hasattr(o.status, "value") else o.status),
                          filled_qty=float(o.filled_qty or 0),
                          filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None)

    def clock_is_open(self) -> bool:
        if not self._client:
            return True
        return bool(self._client.get_clock().is_open)

    # ---- writes (all refuse under DRY_RUN) --------------------------------
    def _refuse(self, what: str) -> bool:
        if self.cfg.dry_run or not self._client:
            log.info("DRY_RUN %s", what)
            return True
        return False

    def submit_limit(self, symbol: str, qty: float, side: str, limit_price: float) -> Optional[str]:
        if self._refuse(f"limit {side} {symbol} x{qty} @ {limit_price:.2f}"):
            return None
        if float(qty) != int(qty):
            # Alpaca fractional orders must be market DAY orders.
            return self.submit_market(symbol, qty, side)
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        req = LimitOrderRequest(symbol=symbol, qty=qty,
                                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2))
        o = self._client.submit_order(req)
        log.info("submitted limit %s %s x%s @ %.2f id=%s", side, symbol, qty, limit_price, o.id)
        return str(o.id)

    def submit_market(self, symbol: str, qty: float, side: str) -> Optional[str]:
        if self._refuse(f"market {side} {symbol} x{qty}"):
            return None
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        req = MarketOrderRequest(symbol=symbol, qty=qty,
                                 side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY)
        o = self._client.submit_order(req)
        log.info("submitted market %s %s x%s id=%s", side, symbol, qty, o.id)
        return str(o.id)

    def close_position(self, symbol: str) -> Optional[str]:
        """Flatten whatever the broker actually holds in `symbol`."""
        if self._refuse(f"close_position {symbol}"):
            return None
        o = self._client.close_position(symbol)
        log.info("close_position %s id=%s", symbol, getattr(o, "id", "?"))
        return str(getattr(o, "id", "")) or None

    def cancel_order(self, order_id: str) -> None:
        if self._refuse(f"cancel {order_id}") or not order_id:
            return
        self._client.cancel_order_by_id(order_id)

    def cancel_all(self) -> None:
        if self._refuse("cancel_all"):
            return
        self._client.cancel_orders()
