"""ATR bands: prior-close anchor ± multiples of a Wilder ATR.

The public build ships evenly spaced bands. The multiples a strategy keys on
are a strategy parameter, not part of the framework.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Sequence

import numpy as np
import pandas as pd

BANDS = (0.5, 1.0, 1.5, 2.0)


def wilder_atr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
               period: int = 14) -> float:
    """Wilder's ATR over the full series; returns the last value."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    if n < period + 1:
        raise ValueError(f"need at least {period + 1} bars, got {n}")
    prev_close = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)])
    atr = tr[:period].mean()
    for x in tr[period:]:
        atr = (atr * (period - 1) + x) / period
    return float(atr)


def wilder_atr_series(high: Sequence[float], low: Sequence[float], close: Sequence[float],
                      period: int = 14) -> np.ndarray:
    """Wilder ATR at every bar (NaN until enough true ranges exist)."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    prev_close = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)])
    atr = tr[:period].mean()
    out[period] = atr
    for i in range(period, len(tr)):
        if i > period:
            atr = (atr * (period - 1) + tr[i - 1]) / period
            out[i] = atr
    atr = (atr * (period - 1) + tr[-1]) / period
    out[n - 1] = atr
    return out


@dataclass(frozen=True)
class LevelMap:
    anchor: float
    atr: float
    up: Dict[float, float]
    down: Dict[float, float]

    def level(self, k: float, direction: str) -> float:
        if k == 0:
            return self.anchor
        table = self.up if direction == "long" else self.down
        if k in table:
            return table[k]
        return round(self.anchor + (k if direction == "long" else -k) * self.atr, 4)

    def as_dict(self) -> Dict[str, float]:
        out = {"anchor": self.anchor, "atr": self.atr}
        for k, v in self.up.items():
            out[f"+{k}"] = v
        for k, v in self.down.items():
            out[f"-{k}"] = v
        return out

    def position_in_range(self, price: float) -> float:
        return (price - self.anchor) / self.atr if self.atr else 0.0


def build_levels(anchor: float, atr: float, bands: Sequence[float] = BANDS) -> LevelMap:
    if atr <= 0:
        raise ValueError("atr must be positive")
    up = {k: round(anchor + k * atr, 4) for k in bands}
    down = {k: round(anchor - k * atr, 4) for k in bands}
    return LevelMap(anchor=anchor, atr=atr, up=up, down=down)


def levels_from_daily(daily: pd.DataFrame, period: int = 14) -> LevelMap:
    atr = wilder_atr(daily["high"].values, daily["low"].values, daily["close"].values, period)
    return build_levels(float(daily["close"].iloc[-1]), atr)
