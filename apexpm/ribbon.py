"""EMA stack (fast/mid/slow) on intraday bars."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd


@dataclass(frozen=True)
class Ribbon:
    fast: float
    mid: float
    slow: float

    @property
    def state(self) -> str:
        if self.fast > self.mid > self.slow:
            return "bull"
        if self.fast < self.mid < self.slow:
            return "bear"
        return "mixed"

    def agrees(self, direction: str) -> bool:
        return (direction == "long" and self.state == "bull") or \
               (direction == "short" and self.state == "bear")


def ema(values: Sequence[float], span: int) -> pd.Series:
    return pd.Series(list(values), dtype=float).ewm(span=span, adjust=False).mean()


def ribbon_from_closes(closes: Sequence[float], fast: int = 9, mid: int = 21,
                       slow: int = 50) -> Ribbon:
    if len(closes) < slow:
        raise ValueError(f"need at least {slow} closes, got {len(closes)}")
    return Ribbon(
        fast=float(ema(closes, fast).iloc[-1]),
        mid=float(ema(closes, mid).iloc[-1]),
        slow=float(ema(closes, slow).iloc[-1]),
    )


def folded(prev: Ribbon, cur: Ribbon) -> bool:
    """True when the stack changed state between two bars (a ribbon fold)."""
    return prev.state != cur.state and cur.state != "mixed"
