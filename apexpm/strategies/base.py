"""Strategy interface shared by the live loop and the backtester."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..levels import LevelMap
from ..ribbon import Ribbon


@dataclass(frozen=True)
class Signal:
    setup: str
    symbol: str
    direction: str            # "long" | "short"
    entry: float
    stop: float
    target: float
    max_hold_min: int
    level_targeted: float     # ATR multiple of the target band, e.g. 1.5
    rr: float
    context: Dict[str, object] = field(default_factory=dict)


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    levels: LevelMap
    ribbon_ltf: Ribbon
    ribbon_htf: Ribbon
    ltf_closes: List[float]
    regime: str = "unknown"
    reached_today: Dict[str, float] = field(default_factory=dict)  # "long"/"short" -> max ATR multiple reached BEFORE this bar
    bar_index_today: int = 0          # 0 = first bar of the session
    minutes_since_open: int = 0
    prev_ribbon_ltf: Optional[Ribbon] = None
    prev2_ribbon_ltf: Optional[Ribbon] = None
    bar_open: float = 0.0
    bar_high: float = 0.0
    bar_low: float = 0.0
    prev_close: float = 0.0
    prev2_close: float = 0.0
    bar_volume: float = 0.0
    vol_avg20: float = 0.0            # average volume of the prior 20 bars
    # price structure inputs (completed bars, oldest -> newest, columns open/high/low/close);
    # None when a caller has no bars, in which case structure factors are simply absent
    ltf_ohlc: Optional[object] = None    # np.ndarray (N, 4) of the last ~120 LTF bars incl. the decision bar
    htf_ohlc: Optional[object] = None    # np.ndarray (M, 4) of the last ~80 completed HTF bars
    pd_high: float = 0.0              # prior session high
    pd_low: float = 0.0               # prior session low
    day_open: float = 0.0             # today's first bar open
    # session structure (apexpm.sessions, 2026-11-07): pre-market range and the completed opening range
    pm_high: float = 0.0
    pm_low: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    session: str = ""                 # premarket / am / midday / pm / closed
    # optional live-only overlay (private module, absent in the public build)
    gex: Optional[dict] = None

    def key_levels(self) -> List[float]:
        """Levels price is expected to react to: the ATR levels, the anchor, prior-day high/low, today's
        open, the pre-market and opening-range extremes. Zeros skipped."""
        lv = self.levels
        out = list(lv.up.values()) + list(lv.down.values()) + [lv.anchor, self.pd_high, self.pd_low, self.day_open,
                                                               self.pm_high, self.pm_low, self.or_high, self.or_low]
        return [float(x) for x in out if x]


class _Params:
    """cfg view with per-strategy overrides so two variants of one strategy
    can run side by side under the same global config."""

    def __init__(self, cfg, overrides: Dict[str, object]):
        self._cfg = cfg
        self._o = overrides

    def __getattr__(self, k):
        if k in self._o:
            return self._o[k]
        return getattr(self._cfg, k)


class Strategy:
    name = "base"
    description = ""
    overrides: Dict[str, object] = {}
    tags: tuple = ()                  # display tags shown wherever the strategy appears, e.g. ("24/5",)

    def __init__(self, name: Optional[str] = None, overrides: Optional[Dict[str, object]] = None,
                 description: Optional[str] = None):
        if name:
            self.name = name
        if overrides:
            self.overrides = dict(overrides)
        if description:
            self.description = description

    def params(self, cfg):
        return _Params(cfg, self.overrides) if self.overrides else cfg

    def knob(self, cfg, key: str, default):
        """Tunable read: this strategy's override, else the global config, else the class default."""
        if key in self.overrides:
            return self.overrides[key]
        return getattr(cfg, key, default)

    def session_ok(self, snap: MarketSnapshot, cfg) -> Optional[str]:
        """Per-strategy session knobs applied the same way live and in the backtester:
        entry_before_min  no new signal after N minutes into the session
        entry_after_min   no new signal before N minutes into the session
        Returns a rejection reason or None."""
        before = self.knob(cfg, "entry_before_min", None)
        after = self.knob(cfg, "entry_after_min", None)
        if before is not None and snap.minutes_since_open > int(before):
            return "entry_window"
        if after is not None and snap.minutes_since_open < int(after):
            return "entry_window"
        return None

    def conviction_min(self, cfg) -> int:
        """Per-strategy conviction floor (override), never below the global floor."""
        return max(int(getattr(cfg, "conviction_min", 0) or 0), int(self.overrides.get("conviction_min", 0) or 0))

    def evaluate(self, snap: MarketSnapshot, cfg) -> tuple:
        """Return (Signal | None, rejection_reason | None)."""
        raise NotImplementedError
