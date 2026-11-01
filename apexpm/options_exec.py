"""Options execution: turn an underlying Signal into a contract position.

OptionsWrapper is a Strategy that wraps another strategy: it evaluates the
inner strategy on the underlying snapshot and, when it fires, chooses a
contract from the live chain (nearest liquid strike to the entry, nearest
expiry inside [min_dte, max_dte]) and returns a Signal whose entry/stop/
target are in PREMIUM terms, with the contract in context.

Exits for option positions (handled by the loop's option exit path):
  premium stop   mark <= entry_prem * (1 - OPT_PREMIUM_STOP)
  premium target mark >= entry_prem * (1 + OPT_PREMIUM_TARGET)   (then runner trails)
  underlying     the inner strategy's stop/target on the underlying price still apply
  time           max hold, flat-by, and never into the close on expiry day
Sizing is contracts = floor(dollar_risk / (entry_prem * 100 * stop_pct)), at
least 1 when the premium fits inside OPT_MAX_PREMIUM_PCT of equity, else skip.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Tuple

from .options import pick_contract
from .strategies.base import MarketSnapshot, Signal, Strategy


class OptionsWrapper(Strategy):
    def __init__(self, inner: Strategy, chain_fn, name: Optional[str] = None, min_dte: int = 0, max_dte: int = 2,
                 min_open_interest: int = 0, max_spread_pct: float = 0.08):
        super().__init__(name=name or f"{inner.name}_opt",
                         description=f"{inner.description} → nearest liquid contract, {min_dte}-{max_dte} DTE")
        self.inner = inner
        self.chain_fn = chain_fn          # callable(underlying, expiry_from, expiry_to) -> List[Contract]
        self.min_dte, self.max_dte = min_dte, max_dte
        self.min_oi, self.max_spread = min_open_interest, max_spread_pct

    def evaluate(self, snap: MarketSnapshot, cfg) -> Tuple[Optional[Signal], Optional[str]]:
        sig, why = self.inner.evaluate(snap, cfg)
        if sig is None:
            return None, why
        today = date.today()
        try:
            chain = self.chain_fn(snap.symbol, today + timedelta(days=self.min_dte), today + timedelta(days=self.max_dte + 3))
        except Exception as exc:  # noqa: BLE001
            return None, f"chain_error:{type(exc).__name__}"
        pick = pick_contract(chain, direction=sig.direction, entry=sig.entry, target=sig.target, today=today,
                             min_dte=self.min_dte, max_dte=self.max_dte, max_spread_pct=self.max_spread,
                             min_open_interest=self.min_oi)
        if pick is None:
            return None, "no_liquid_contract"
        c = pick.contract
        prem = c.ask if c.ask > 0 else c.mid
        if prem <= 0.05:
            return None, "premium_too_low"
        stop_pct = getattr(cfg, "opt_premium_stop", 0.5)
        tgt_pct = getattr(cfg, "opt_premium_target", 1.0)
        ctx = dict(sig.context)
        ctx.update({"asset": "option", "contract": c.symbol, "strike": c.strike, "expiry": c.expiry.isoformat(),
                    "option_type": c.type, "under_entry": sig.entry, "under_stop": sig.stop, "under_target": sig.target,
                    "breakeven": pick.breakeven, "fill_at_market": True, "inner": self.inner.name})
        return Signal(setup=self.name, symbol=snap.symbol, direction=sig.direction,
                      entry=round(prem, 2), stop=round(prem * (1 - stop_pct), 2), target=round(prem * (1 + tgt_pct), 2),
                      max_hold_min=sig.max_hold_min, level_targeted=sig.level_targeted,
                      rr=round(tgt_pct / stop_pct, 2), context=ctx), None


def size_contracts(equity: float, premium: float, stop_pct: float, risk_pct: float, max_premium_pct: float) -> Tuple[int, Optional[str]]:
    """Contracts for a given dollar-risk budget; at least one if it fits the premium cap."""
    if premium <= 0:
        return 0, "bad_premium"
    risk_per = premium * 100 * stop_pct + 1.3
    n = int((equity * risk_pct) // risk_per)
    n = max(n, 1)
    cap = int((equity * max_premium_pct) // (premium * 100))
    if cap < 1:
        return 0, "premium_exceeds_cap"
    return min(n, cap), None
