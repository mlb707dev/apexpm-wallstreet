"""Exit engine shared by the backtester and the live loop.

Rules (all configurable):
  breakeven   once unrealized >= EXIT_BE_AT_R risk units, stop moves to entry + EXIT_BE_OFFSET_ATR
  runner      when the target is reached and momentum agrees (5m ribbon in trade direction),
              do not sell: stop ratchets to (target - EXIT_TRAIL_ATR) and the target moves to the
              next level up the map. Repeats level by level. Without momentum, take the target.
  trailing    after the runner starts, every new level reached ratchets the stop to the prior
              level minus EXIT_TRAIL_ATR.
Stops only ever move in the trade's favour. Hard exits (max hold, flat-by) are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ExitPlan:
    direction: str
    entry: float
    initial_stop: float
    stop: float
    target: float
    atr: float
    levels_up: List[float]      # absolute prices of levels in the trade direction, ascending in favour
    runner: bool = False
    be_done: bool = False

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.initial_stop)


def next_level(plan: ExitPlan, beyond: float) -> Optional[float]:
    """First level strictly more favourable than `beyond`."""
    s = plan.sign
    cands = [lv for lv in plan.levels_up if (lv - beyond) * s > 1e-9]
    if not cands:
        return None
    return min(cands, key=lambda lv: (lv - beyond) * s)


def update(plan: ExitPlan, hi: float, lo: float, momentum: bool, cfg) -> Tuple[Optional[str], Optional[float], bool]:
    """Apply the rules for one bar/tick. Returns (exit_reason, exit_price, changed).

    hi/lo: the bar's extremes (for a live tick pass price for both).
    """
    s = plan.sign
    best = hi if s == 1 else lo          # most favourable print this bar
    worst = lo if s == 1 else hi
    changed = False

    # 1. stop hit (checked first: conservative)
    if (worst - plan.stop) * s <= 0:
        return "stop" if not plan.runner and not plan.be_done else ("trail_stop" if plan.runner else "breakeven_stop"), plan.stop, changed

    # 2. breakeven
    be_at = getattr(cfg, "exit_be_at_r", 1.0)
    if not plan.be_done and be_at > 0 and (best - plan.entry) * s >= be_at * plan.risk:
        new_stop = plan.entry + s * getattr(cfg, "exit_be_offset_atr", 0.02) * plan.atr
        if (new_stop - plan.stop) * s > 0:
            plan.stop = new_stop
            changed = True
        plan.be_done = True

    # 3. target reached
    if (best - plan.target) * s >= 0:
        if getattr(cfg, "exit_runner", True) and momentum:
            nxt = next_level(plan, plan.target)
            if nxt is not None:
                new_stop = plan.target - s * getattr(cfg, "exit_trail_atr", 0.1) * plan.atr
                if (new_stop - plan.stop) * s > 0:
                    plan.stop = new_stop
                plan.target = nxt
                plan.runner = True
                return None, None, True
        return ("runner_target" if plan.runner else "target"), plan.target, changed
    return None, None, changed
