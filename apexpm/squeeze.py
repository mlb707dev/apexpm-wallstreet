"""TTM Squeeze, the volatility-compression read on the owner's TradingView chart (2026-11-07).

  squeeze on    Bollinger Bands (20, 2.0) sit inside the Keltner Channel (20, 1.5 x ATR): the market is
                coiling and a directional move usually follows
  squeeze off   the bands have expanded back outside the channel: the move is under way
  fire          the first bar the squeeze turns off after at least MIN_ON bars on
  momentum      linear-regression momentum of close against the midpoint of the Donchian mid and the
                20-bar average (the "histogram"); its sign is the direction, rising means strengthening

Pure numpy over OHLC arrays so the live loop and the backtester compute the same thing.
"""
from __future__ import annotations

from typing import Dict, Sequence

import numpy as np

LENGTH = 20
BB_MULT = 2.0
KC_MULT = 1.5
MIN_ON = 6


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    for i in range(n - 1, len(x)):
        out[i] = np.std(x[i - n + 1:i + 1])
    return out


def _true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    pc = np.concatenate([[c[0]], c[:-1]])
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def _linreg_last(y: np.ndarray) -> float:
    n = len(y)
    x = np.arange(n, dtype=float)
    xm, ym = x.mean(), y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom == 0:
        return 0.0
    slope = ((x - xm) * (y - ym)).sum() / denom
    return float(ym + slope * (x[-1] - xm))


def compute(high: Sequence[float], low: Sequence[float], close: Sequence[float],
            length: int = LENGTH, bb_mult: float = BB_MULT, kc_mult: float = KC_MULT) -> Dict[str, np.ndarray]:
    h, l, c = (np.asarray(v, dtype=float) for v in (high, low, close))
    n = len(c)
    mid = _sma(c, length)
    sd = _std(c, length)
    tr = _true_range(h, l, c)
    atr = _sma(tr, length)
    bb_up, bb_dn = mid + bb_mult * sd, mid - bb_mult * sd
    kc_up, kc_dn = mid + kc_mult * atr, mid - kc_mult * atr
    on = (bb_up < kc_up) & (bb_dn > kc_dn)
    on = np.where(np.isnan(mid), False, on)
    mom = np.full(n, np.nan)
    for i in range(length - 1, n):
        win = slice(i - length + 1, i + 1)
        donchian_mid = (h[win].max() + l[win].min()) / 2.0
        base = (donchian_mid + mid[i]) / 2.0
        mom[i] = _linreg_last(c[win] - base)
    return {"on": on, "momentum": mom, "mid": mid}


def state(high, low, close) -> Dict[str, object]:
    """The last bar's read: on/off, bars in the current state, fired this bar, momentum and its slope."""
    s = compute(high, low, close)
    on, mom = s["on"], s["momentum"]
    n = len(on)
    if n < LENGTH + 2 or np.isnan(mom[-1]):
        return {"ready": False}
    run = 1
    while run < n and on[-1 - run] == on[-1]:
        run += 1
    prev_on_run = 0
    if not on[-1]:
        i = n - 1 - run
        while i >= 0 and on[i]:
            prev_on_run += 1
            i -= 1
    fired = (not on[-1]) and run == 1 and prev_on_run >= MIN_ON
    return {"ready": True, "on": bool(on[-1]), "bars_in_state": int(run), "fired": bool(fired),
            "momentum": float(mom[-1]), "rising": bool(mom[-1] > mom[-2]), "prev_on_run": int(prev_on_run)}


def factors(direction: str, high, low, close) -> Dict[str, bool]:
    """Yes/no inputs for the conviction gate:
      squeeze_fire        the squeeze released within the last 3 bars with momentum in the trade direction
      momentum_aligned    momentum is on the trade's side and strengthening
    """
    s = compute(high, low, close)
    on, mom = s["on"], s["momentum"]
    n = len(on)
    if n < LENGTH + 4 or np.isnan(mom[-1]):
        return {"squeeze_fire": False, "momentum_aligned": False}
    sgn = 1 if direction == "long" else -1
    fired_recent = False
    for back in range(0, 3):
        i = n - 1 - back
        if i < MIN_ON + 1:
            break
        if not on[i] and on[i - 1] and all(on[i - 1 - k] for k in range(MIN_ON)):
            fired_recent = True
            break
    return {"squeeze_fire": bool(fired_recent and mom[-1] * sgn > 0),
            "momentum_aligned": bool(mom[-1] * sgn > 0 and (mom[-1] - mom[-2]) * sgn > 0)}
