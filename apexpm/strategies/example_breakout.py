"""Example strategy: ATR band breakout with an EMA-stack filter.

This is a teaching example that exercises the framework (levels, EMA stack,
signal contract, risk layer, backtester). It is not a tuned or recommended
strategy. Replace it with your own research.
"""
from __future__ import annotations

from typing import Optional, Tuple

from .base import MarketSnapshot, Signal, Strategy


class ExampleBreakout(Strategy):
    name = "example_breakout"
    description = "Close beyond ±1.0 ATR from the prior close with the EMA stack agreeing; target 1.5, stop 0.5"

    entry_band = 1.0
    target_band = 1.5
    stop_band = 0.5
    min_rr = 1.0
    max_hold_min = 120

    def evaluate(self, snap: MarketSnapshot, cfg) -> Tuple[Optional[Signal], Optional[str]]:
        lv = snap.levels
        pos = lv.position_in_range(snap.price)
        if pos >= self.entry_band:
            direction = "long"
        elif pos <= -self.entry_band:
            direction = "short"
        else:
            return None, "not_at_entry_level"
        if snap.reached_today.get(direction, 0.0) >= self.target_band:
            return None, "target_already_hit_today"
        if not snap.ribbon_htf.agrees(direction):
            return None, "ribbon_htf_disagrees"
        entry = lv.level(self.entry_band, direction)
        target = lv.level(self.target_band, direction)
        stop = lv.level(self.stop_band, direction)
        risk, reward = abs(entry - stop), abs(target - entry)
        rr = reward / risk if risk else 0.0
        if rr < self.min_rr:
            return None, "rr_too_low"
        return Signal(setup=self.name, symbol=snap.symbol, direction=direction,
                      entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
                      max_hold_min=self.max_hold_min, level_targeted=self.target_band, rr=round(rr, 3),
                      context={"pos_atr": round(pos, 3), "ribbon_htf": snap.ribbon_htf.state, "regime": snap.regime}), None
