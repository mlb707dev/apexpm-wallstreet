"""Conviction gate: a trade needs several independent technical confirmations.

Computed from the 5-minute closes available at decision time (completed bars
only) plus the snapshot. Each factor is a yes/no; the score is the count.
The loop refuses any signal below CONVICTION_MIN and records which factors
fired, so the journal can later show which confirmations actually mattered.

Factors (owner's list, 2026-11-01):
  level_break_volume   this bar crossed a daily ATR level in the trade direction with volume >= 1.2x the slot average
  ema_cross            fast EMA crossed the mid EMA in the trade direction within the last 3 bars
  htf_stack            15m EMA stack agrees
  macd                 MACD histogram turned (crossed zero) in the trade direction within the last 3 bars, or is on the right side and rising
  rsi                  RSI(14) crossed 50 in the direction within the last 3 bars, or left oversold/overbought in the direction
  stoch                stochastic %K crossed %D in the direction from the 20/80 zones within the last 3 bars
  anchor_side          price on the trade's side of the daily anchor
Structure factors (2026-11-06, from apexpm.structure; present only when the snapshot carries OHLC bars):
  bos_aligned          latest 5m break of structure is in the trade direction
  htf_structure        15m swing trend (higher highs/lows or lower) agrees
  ob_retest            price sits in a fresh order block of the trade direction
  fvg_support          price sits in an open fair value gap of the trade direction
  level_reaction       this bar rejected a key level (ATR level, anchor, prior-day high/low, open) in the trade direction
Squeeze factors (2026-11-07, from apexpm.squeeze, the TTM Squeeze on the owner's chart; need OHLC bars):
  squeeze_fire         the volatility squeeze released within the last 3 bars with momentum in the trade direction
  momentum_aligned     squeeze momentum is on the trade's side and strengthening
Private overlays may add further factors in the live build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd


@dataclass
class Conviction:
    score: int
    factors: Dict[str, bool] = field(default_factory=dict)

    @property
    def fired(self) -> List[str]:
        return [k for k, v in self.factors.items() if v]


def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False).mean()


def indicators(closes: Sequence[float], highs: Sequence[float] = None, lows: Sequence[float] = None) -> pd.DataFrame:
    c = pd.Series(list(closes), dtype=float)
    out = pd.DataFrame({"close": c})
    out["ema_fast"] = _ema(c, 8)
    out["ema_mid"] = _ema(c, 21)
    macd = _ema(c, 12) - _ema(c, 26)
    out["macd_hist"] = macd - _ema(macd, 9)
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = up / (dn + 1e-12)
    out["rsi"] = 100 - 100 / (1 + rs)
    h = pd.Series(list(highs), dtype=float) if highs is not None else c
    l = pd.Series(list(lows), dtype=float) if lows is not None else c
    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    k = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    out["stoch_k"] = k.rolling(3).mean()
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()
    return out


def score(direction: str, snap, closes: Sequence[float], highs=None, lows=None, lookback: int = 3) -> Conviction:
    s = 1 if direction == "long" else -1
    ind = indicators(closes, highs, lows)
    if len(ind) < 30:
        return Conviction(0, {})
    last = ind.iloc[-1]
    win = ind.iloc[-(lookback + 1):]
    f: Dict[str, bool] = {}

    # level break with volume: the bar's range crossed a level in the direction, volume strong
    lv = snap.levels
    levels = list(lv.up.values()) + list(lv.down.values()) + [lv.anchor]
    crossed = any((snap.bar_low <= L <= snap.bar_high) and ((snap.price - L) * s > 0) and ((snap.bar_open - L) * s <= 0) for L in levels)
    vol_ok = snap.vol_avg20 > 0 and snap.bar_volume >= 1.2 * snap.vol_avg20
    f["level_break_volume"] = bool(crossed and vol_ok)

    diff = (ind["ema_fast"] - ind["ema_mid"]) * s
    f["ema_cross"] = bool((diff.iloc[-1] > 0) and (diff.iloc[-(lookback + 1):-1] <= 0).any())

    f["htf_stack"] = bool(snap.ribbon_htf.agrees(direction))

    mh = ind["macd_hist"] * s
    f["macd"] = bool((mh.iloc[-1] > 0 and (mh.iloc[-(lookback + 1):-1] <= 0).any()) or
                     (mh.iloc[-1] > 0 and mh.iloc[-1] > mh.iloc[-2] > mh.iloc[-3]))

    r = ind["rsi"]
    if s == 1:
        f["rsi"] = bool((r.iloc[-1] > 50 and (r.iloc[-(lookback + 1):-1] <= 50).any()) or (r.iloc[-1] > 30 and (r.iloc[-(lookback + 1):-1] <= 30).any()))
    else:
        f["rsi"] = bool((r.iloc[-1] < 50 and (r.iloc[-(lookback + 1):-1] >= 50).any()) or (r.iloc[-1] < 70 and (r.iloc[-(lookback + 1):-1] >= 70).any()))

    k, d = ind["stoch_k"], ind["stoch_d"]
    kd = (k - d) * s
    zone = (k.iloc[-(lookback + 1):-1] <= 20).any() if s == 1 else (k.iloc[-(lookback + 1):-1] >= 80).any()
    f["stoch"] = bool(kd.iloc[-1] > 0 and (kd.iloc[-(lookback + 1):-1] <= 0).any() and zone)

    f["anchor_side"] = bool((snap.price - lv.anchor) * s > 0)

    arr = getattr(snap, "ltf_ohlc", None)
    if arr is not None and len(arr) >= 9 and lv.atr > 0:
        from .structure import factors as structure_factors
        a = np.asarray(arr, dtype=float)
        hx = np.asarray(snap.htf_ohlc, dtype=float) if getattr(snap, "htf_ohlc", None) is not None else None
        f.update(structure_factors(direction, a[:, 0], a[:, 1], a[:, 2], a[:, 3], lv.atr, snap.key_levels(),
                                   htf_high=hx[:, 1] if hx is not None else None,
                                   htf_low=hx[:, 2] if hx is not None else None))
        if len(a) >= 30:
            from .squeeze import factors as squeeze_factors
            f.update(squeeze_factors(direction, a[:, 1], a[:, 2], a[:, 3]))
    return Conviction(sum(f.values()), f)
