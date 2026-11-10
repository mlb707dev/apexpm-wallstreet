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
    shorting_enabled: bool = True


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
                       buying_power=float(a.buying_power),
                       shorting_enabled=bool(getattr(a, "shorting_enabled", True)))

    def cash_activities(self, after: Optional[str] = None) -> List[dict]:
        """Deposits (CSD) and withdrawals (CSW) from the account activities, newest last.
        Each: {id, ts, amount (+in/-out), note}. Empty when not connected."""
        if not self._client:
            return []
        from alpaca.trading.enums import ActivityType
        from alpaca.trading.requests import GetAccountActivitiesRequest
        out = []
        for kind in (ActivityType.CSD, ActivityType.CSW):
            req = GetAccountActivitiesRequest(activity_types=[kind])
            for a in self._client.get_account_activities(req) or []:
                amt = float(getattr(a, "net_amount", 0) or 0)
                if kind == ActivityType.CSW and amt > 0:
                    amt = -amt
                ts = getattr(a, "date", None) or getattr(a, "transaction_time", None)
                out.append({"id": str(a.id), "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                            "amount": amt, "note": "deposit" if kind == ActivityType.CSD else "withdrawal"})
        return sorted(out, key=lambda x: x["ts"])

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

    def submit_limit_ext(self, symbol: str, qty: float, side: str, limit_price: float) -> Optional[str]:
        """Extended-hours limit order (overnight desk). Alpaca accepts only limit orders with TIF day/gtc and
        extended_hours=True outside 09:30-16:00 (pre-market, after-hours and the 20:00-04:00 overnight
        session); a DAY order placed overnight carries into that trade date's sessions and is cancelled at
        20:00 ET. Whole shares only here: fractional extended-hours orders need the asset's
        fractional_eh_enabled flag, so the desk sizes in whole shares and never sends a fraction."""
        if float(qty) != int(qty) or qty <= 0:
            raise ValueError(f"extended-hours orders are whole shares only (got {qty})")
        if self._refuse(f"ext limit {side} {symbol} x{qty} @ {limit_price:.2f}"):
            return None
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        req = LimitOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2), extended_hours=True)
        o = self._client.submit_order(req)
        log.info("submitted ext limit %s %s x%s @ %.2f id=%s", side, symbol, qty, limit_price, o.id)
        return str(o.id)

    def asset_attributes(self, symbol: str) -> List[str]:
        """Asset.attributes (e.g. overnight_tradable, overnight_halted, fractional_eh_enabled); [] offline."""
        if not self._client:
            return []
        a = self._client.get_asset(symbol)
        return list(getattr(a, "attributes", None) or [])

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


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
class OptionsBroker:
    """Option chain lookup and single-leg contract orders. Paper by default;
    DRY_RUN never sends. Contracts are OCC symbols, e.g. SPY261103C00680000."""

    def __init__(self, cfg, broker: "Broker"):
        self.cfg = cfg
        self.broker = broker
        self._data = None
        if cfg.alpaca_api_key and cfg.alpaca_secret_key:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._data = OptionHistoricalDataClient(cfg.alpaca_api_key, cfg.alpaca_secret_key)

    @property
    def connected(self) -> bool:
        return self._data is not None and self.broker.connected

    def chain(self, underlying: str, expiry_from=None, expiry_to=None):
        """Live chain snapshots → list of Contract (apexpm.options)."""
        from datetime import date as _date
        from alpaca.data.requests import OptionChainRequest
        from .options import Contract
        req = OptionChainRequest(underlying_symbol=underlying, feed="indicative",
                                 expiration_date_gte=expiry_from, expiration_date_lte=expiry_to)
        snaps = self._data.get_option_chain(req)
        out = []
        import re
        pat = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
        for sym, snap in snaps.items():
            m = pat.match(sym)
            if not m:
                continue
            q = snap.latest_quote
            if q is None:
                continue
            from datetime import datetime as _dt
            exp = _dt.strptime(m.group(2), "%y%m%d").date()
            oi = int(getattr(snap, "open_interest", 0) or 0)
            out.append(Contract(symbol=sym, strike=int(m.group(4)) / 1000, expiry=exp,
                                type="call" if m.group(3) == "C" else "put",
                                bid=float(q.bid_price or 0), ask=float(q.ask_price or 0), open_interest=oi))
        return out

    def buy_to_open(self, occ: str, qty: int, limit: Optional[float] = None) -> Optional[str]:
        if self.cfg.dry_run or not self.broker.connected:
            log.info("DRY_RUN buy_to_open %s x%d @ %s", occ, qty, limit)
            return None
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        if limit:
            req = LimitOrderRequest(symbol=occ, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                    limit_price=round(limit, 2))
        else:
            req = MarketOrderRequest(symbol=occ, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        o = self.broker._client.submit_order(req)
        log.info("option buy_to_open %s x%d id=%s", occ, qty, o.id)
        return str(o.id)

    def sell_to_close(self, occ: str, qty: int, limit: Optional[float] = None) -> Optional[str]:
        if self.cfg.dry_run or not self.broker.connected:
            log.info("DRY_RUN sell_to_close %s x%d @ %s", occ, qty, limit)
            return None
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        if limit:
            req = LimitOrderRequest(symbol=occ, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                    limit_price=round(limit, 2))
        else:
            req = MarketOrderRequest(symbol=occ, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        o = self.broker._client.submit_order(req)
        log.info("option sell_to_close %s x%d id=%s", occ, qty, o.id)
        return str(o.id)

    def greeks(self, occ: str) -> Optional[dict]:
        """Snapshot greeks + IV for one contract (Alpaca computes them on the indicative feed). None on failure."""
        from alpaca.data.requests import OptionSnapshotRequest
        try:
            snaps = self._data.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=occ, feed="indicative"))
            snap = snaps.get(occ)
            if snap is None:
                return None
            g = getattr(snap, "greeks", None)
            out = {k: round(float(getattr(g, k)), 4) for k in ("delta", "gamma", "theta", "vega", "rho")
                   if g is not None and getattr(g, k, None) is not None}
            iv = getattr(snap, "implied_volatility", None)
            if iv is not None:
                out["iv"] = round(float(iv), 4)
            q = getattr(snap, "latest_quote", None)
            if q is not None and q.bid_price and q.ask_price:
                out["bid"], out["ask"] = float(q.bid_price), float(q.ask_price)
            return out or None
        except Exception as exc:  # noqa: BLE001
            log.warning("option greeks failed %s: %s", occ, exc)
            return None

    def mark(self, occ: str) -> Optional[float]:
        """Mid of the latest quote, or None."""
        from alpaca.data.requests import OptionLatestQuoteRequest
        try:
            q = self._data.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=occ, feed="indicative"))
            quote = q.get(occ)
            if not quote or not quote.bid_price or not quote.ask_price:
                return None
            return (float(quote.bid_price) + float(quote.ask_price)) / 2
        except Exception as exc:  # noqa: BLE001
            log.warning("option mark failed %s: %s", occ, exc)
            return None
