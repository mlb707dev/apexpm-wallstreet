"""Interface for extended-hours (overnight desk) strategies, shared by the overnight backtester and the
overnight desk in the loop. Kept apart from the regular-hours MarketSnapshot on purpose: these strategies
measure from the last regular-session close, see only the extended-hours tape of the current window, and
never run in the regular-hours scan (the loop skips any strategy with `ext_sessions`).

A window runs from one regular close to the next regular open: after-hours, the overnight session, then
pre-market. Orders are limit-only (Alpaca rejects every other type outside 09:30-16:00); a signal's
`entry` is the limit price and `context["exit_by"]` (ISO, UTC) is the hard time exit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

import numpy as np

from .base import Signal, Strategy

__all__ = ["ExtSnapshot", "ExtStrategy", "Signal"]


@dataclass
class ExtSnapshot:
    symbol: str
    now_et: datetime                  # close time of the decision bar, ET
    session: str                      # pre / ah / overnight
    price: float                      # decision bar close
    bar_open: float
    bar_high: float
    bar_low: float
    bar_volume: float
    ref_close: float                  # last regular-session close (the anchor)
    ref_open: float
    ref_high: float
    ref_low: float
    atr: float                        # Wilder daily ATR through the anchor session
    prev_ref_close: float = 0.0       # the regular close before the anchor
    daily_closes: Optional[np.ndarray] = None     # regular closes up to and including the anchor (oldest first)
    minutes_into_session: int = 0
    next_open_et: Optional[datetime] = None
    # this window's bars so far, decision bar last: columns open, high, low, close, volume
    win: Optional[np.ndarray] = None
    win_sessions: Optional[np.ndarray] = None     # session label per row of `win`
    win_minutes: Optional[np.ndarray] = None      # ET minute-of-day per row of `win`
    session_vol: float = 0.0          # volume traded in this session so far
    typical_session_vol: float = 0.0  # median of the prior 20 windows' volume in this session up to the same minute
    shortable: bool = False
    context: dict = field(default_factory=dict)

    @property
    def move_atr(self) -> float:
        """Distance from the last regular close, in ATRs (positive = above)."""
        return (self.price - self.ref_close) / self.atr if self.atr else 0.0

    @property
    def rel_volume(self) -> float:
        return self.session_vol / self.typical_session_vol if self.typical_session_vol else 0.0

    def rows(self, session: Optional[str] = None, from_min: Optional[int] = None, to_min: Optional[int] = None,
             include_current: bool = True) -> np.ndarray:
        """Window rows filtered by session and ET minute-of-day [from_min, to_min)."""
        if self.win is None or len(self.win) == 0:
            return np.empty((0, 5))
        m = np.ones(len(self.win), dtype=bool)
        if not include_current:
            m[-1] = False
        if session is not None and self.win_sessions is not None:
            m &= self.win_sessions == session
        if self.win_minutes is not None:
            if from_min is not None:
                m &= self.win_minutes >= from_min
            if to_min is not None:
                m &= self.win_minutes < to_min
        return self.win[m]


def hhmm(s: str) -> int:
    h, m = (int(x) for x in s.split(":"))
    return h * 60 + m


class ExtStrategy(Strategy):
    """Base for overnight-desk strategies. `ext_sessions` names the sessions it may enter in."""
    ext_sessions: Tuple[str, ...] = ("ah",)
    family = "overnight"
    tags = ("24/5",)                  # shown on every card, letter and decision: these trade outside 09:30-16:00

    def directions(self, cfg, key: str):
        d = self.knob(cfg, key, ["long"])
        return list(d) if d else ["long"]

    def session_ok(self, snap, cfg) -> Optional[str]:
        sessions = self.knob(cfg, "ext_sessions_override", None) or self.ext_sessions
        if getattr(snap, "session", None) not in sessions:
            return "ext_session"
        return None

    def decision_window(self, cfg):
        """Optional speed hint for the backtester: ET minute-of-day ranges [(lo, hi)] (inclusive, on the
        decision bar's CLOSE time) outside which evaluate() can never signal. None = no hint. evaluate()
        still applies its own window, so the hint never changes results, only skips work."""
        return None

    def evaluate(self, snap: ExtSnapshot, cfg) -> tuple:
        raise NotImplementedError
