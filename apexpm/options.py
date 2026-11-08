"""ATR-anchored strike selection. Pure functions over a chain snapshot.

A chain row needs: symbol, strike, expiry (date), type ('call'|'put'),
bid, ask, open_interest. Alpaca's option chain feeds this; tests use fakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class Contract:
    symbol: str
    strike: float
    expiry: date
    type: str
    bid: float
    ask: float
    open_interest: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid else 1.0


@dataclass(frozen=True)
class StrikeChoice:
    contract: Contract
    breakeven: float
    reason: str


def pick_contract(chain: Iterable[Contract], *, direction: str, entry: float, target: float,
                  today: date, min_dte: int = 1, max_dte: int = 7, max_spread_pct: float = 0.05,
                  min_open_interest: int = 500, anchor_level: Optional[float] = None) -> Optional[StrikeChoice]:
    """Nearest liquid strike to the anchor level (default: entry), on the nearest
    expiry inside [min_dte, max_dte], whose breakeven stays inside the target.

    Long → call, short → put. Returns None with no eligible contract.
    """
    want = "call" if direction == "long" else "put"
    anchor = anchor_level if anchor_level is not None else entry
    rows = [c for c in chain if c.type == want and min_dte <= (c.expiry - today).days <= max_dte]
    if not rows:
        return None
    nearest_exp = min(r.expiry for r in rows)
    rows = [r for r in rows if r.expiry == nearest_exp]
    liquid = [r for r in rows if r.spread_pct <= max_spread_pct and r.open_interest >= min_open_interest and r.mid > 0]
    if not liquid:
        return None
    liquid.sort(key=lambda r: (abs(r.strike - anchor), r.strike))
    for c in liquid:
        be = c.strike + c.ask if want == "call" else c.strike - c.ask
        inside = (be < target) if want == "call" else (be > target)
        if inside:
            return StrikeChoice(contract=c, breakeven=round(be, 2),
                                reason=f"nearest liquid {want} to {anchor:.2f}, breakeven {be:.2f} inside target {target:.2f}")
    return None


def contracts_for_risk(dollar_risk: float, premium: float, multiplier: int = 100) -> int:
    """Whole contracts such that premium at risk <= dollar_risk."""
    if premium <= 0:
        return 0
    return int(dollar_risk // (premium * multiplier))
