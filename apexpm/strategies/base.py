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

    def evaluate(self, snap: MarketSnapshot, cfg) -> tuple:
        """Return (Signal | None, rejection_reason | None)."""
        raise NotImplementedError
