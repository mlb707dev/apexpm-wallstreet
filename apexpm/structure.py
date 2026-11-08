"""Price structure read from completed OHLC bars: swings, breaks of structure, order blocks,
fair value gaps and candle reactions at key levels.

Pure functions over numpy arrays (oldest -> newest, completed bars only), so the live loop and
the backtester see exactly the same thing. Nothing here knows about strategies or sizing; the
strategies and the conviction gate ask these questions:

  swings(h, l, k)                     fractal swing highs/lows
  trend_from_swings(sw)               "up" (HH+HL), "down" (LH+LL) or "range"
  last_break(c, sw)                   latest break of structure: ("up"|"down", bar index) or None
  order_blocks(o, h, l, c, atr, ...)  bullish/bearish order blocks and whether price has mitigated them
  fair_value_gaps(h, l, ...)          three-candle imbalances still open
  in_zone(price, zone)                price inside a zone
  rejection(o, h, l, c, level, dir)   candle traded through a level and closed back on the trade's side
  factors(direction, bars, ...)       the yes/no structure confirmations the conviction gate counts
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class Swing:
    idx: int
    price: float
    kind: str          # "high" | "low"


@dataclass(frozen=True)
class Zone:
    kind: str          # "bull_ob" | "bear_ob" | "bull_fvg" | "bear_fvg"
    top: float
    bottom: float
    idx: int           # bar that created it
    mitigated: bool    # price has traded back into it since

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom


OB_LOOKAHEAD = 6     # bars after a candidate candle in which the impulse must play out


def _arr(x) -> np.ndarray:
    return np.asarray(list(x), dtype=float)


# ---- swings and structure -------------------------------------------------------------
def swings(high: Sequence[float], low: Sequence[float], k: int = 3) -> List[Swing]:
    """Fractal swings: a high above the k bars on each side is a swing high; lows mirror.
    The last k bars can never be confirmed swings (no right side yet)."""
    h, l = _arr(high), _arr(low)
    n = len(h)
    out: List[Swing] = []
    for i in range(k, n - k):
        if h[i] >= h[i - k:i].max() and h[i] > h[i + 1:i + k + 1].max():
            out.append(Swing(i, float(h[i]), "high"))
        if l[i] <= l[i - k:i].min() and l[i] < l[i + 1:i + k + 1].min():
            out.append(Swing(i, float(l[i]), "low"))
    out.sort(key=lambda s: s.idx)
    return out


def trend_from_swings(sw: Sequence[Swing]) -> str:
    """'up' when the last two swing highs and the last two swing lows both rise, 'down' when both
    fall, else 'range'."""
    highs = [s.price for s in sw if s.kind == "high"][-2:]
    lows = [s.price for s in sw if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "range"
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "up"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "down"
    return "range"


def last_break(close: Sequence[float], sw: Sequence[Swing]) -> Optional[Tuple[str, int]]:
    """Most recent close beyond the most recent confirmed swing of the opposite kind: a close above
    the last swing high is an "up" break, below the last swing low a "down" break."""
    c = _arr(close)
    best: Optional[Tuple[str, int]] = None
    for s in sw:
        after = c[s.idx + 1:]
        if not len(after):
            continue
        if s.kind == "high":
            hit = np.nonzero(after > s.price)[0]
            if len(hit):
                i = s.idx + 1 + int(hit[0])
                if best is None or i > best[1]:
                    best = ("up", i)
        else:
            hit = np.nonzero(after < s.price)[0]
            if len(hit):
                i = s.idx + 1 + int(hit[0])
                if best is None or i > best[1]:
                    best = ("down", i)
    return best


# ---- zones ------------------------------------------------------------------------------
def order_blocks(open_: Sequence[float], high: Sequence[float], low: Sequence[float], close: Sequence[float],
                 atr: float, min_impulse_atr: float = 1.0, lookahead: int = OB_LOOKAHEAD, max_age: int = 120) -> List[Zone]:
    """A bullish order block is the last down candle before an impulse up of at least
    min_impulse_atr (measured from that candle's low over the next `lookahead` bars) that also
    closes above the candle's high; bearish mirrors. The zone is the candle's range. A block is
    mitigated once a later bar trades into it. Only blocks younger than max_age bars are kept."""
    o, h, l, c = _arr(open_), _arr(high), _arr(low), _arr(close)
    n = len(c)
    if atr <= 0 or n < 3:
        return []
    out: List[Zone] = []
    start = max(1, n - max_age)
    for i in range(start, n - 1):
        end = min(n, i + 1 + lookahead)
        if c[i] < o[i]:                                   # down candle: bullish OB candidate
            if (h[i + 1:end].max() - l[i]) >= min_impulse_atr * atr and (c[i + 1:end] > h[i]).any():
                mit = bool((l[i + 1 + lookahead:] <= h[i]).any()) if i + 1 + lookahead < n else False
                out.append(Zone("bull_ob", float(h[i]), float(l[i]), i, mit))
        elif c[i] > o[i]:                                 # up candle: bearish OB candidate
            if (h[i] - l[i + 1:end].min()) >= min_impulse_atr * atr and (c[i + 1:end] < l[i]).any():
                mit = bool((h[i + 1 + lookahead:] >= l[i]).any()) if i + 1 + lookahead < n else False
                out.append(Zone("bear_ob", float(h[i]), float(l[i]), i, mit))
    return out


def fair_value_gaps(high: Sequence[float], low: Sequence[float], atr: float, min_gap_atr: float = 0.1,
                    max_age: int = 120) -> List[Zone]:
    """Three-candle imbalance: bar i-1's low above bar i-3's high (bullish) or its high below
    bar i-3's low (bearish). Filled (mitigated) once a later bar trades through the gap."""
    h, l = _arr(high), _arr(low)
    n = len(h)
    out: List[Zone] = []
    for i in range(max(2, n - max_age), n):
        if l[i] - h[i - 2] >= min_gap_atr * atr:
            mit = bool((l[i + 1:] <= h[i - 2]).any())
            out.append(Zone("bull_fvg", float(l[i]), float(h[i - 2]), i, mit))
        elif l[i - 2] - h[i] >= min_gap_atr * atr:
            mit = bool((h[i + 1:] >= l[i - 2]).any())
            out.append(Zone("bear_fvg", float(l[i - 2]), float(h[i]), i, mit))
    return out


def in_zone(price: float, z: Zone, tol: float = 0.0) -> bool:
    return (z.bottom - tol) <= price <= (z.top + tol)


def fresh_zone(zones: Sequence[Zone], kind: str, price: float, tol: float = 0.0) -> Optional[Zone]:
    """Youngest unmitigated zone of `kind` that contains price."""
    for z in sorted(zones, key=lambda z: -z.idx):
        if z.kind == kind and not z.mitigated and in_zone(price, z, tol):
            return z
    return None


# ---- reactions at levels ----------------------------------------------------------------
def rejection(open_: float, high: float, low: float, close: float, level: float, direction: str,
              wick_ratio: float = 0.5, tol: float = 0.0) -> bool:
    """The bar traded through `level` against the trade and closed back on the trade's side, with
    the rejecting wick at least wick_ratio of the bar's range. Long: low <= level, close > level,
    lower wick long. Short mirrors."""
    rng = high - low
    if rng <= 0:
        return False
    if direction == "long":
        wick = min(open_, close) - low
        return (low <= level + tol) and (close > level) and (wick / rng >= wick_ratio)
    wick = high - max(open_, close)
    return (high >= level - tol) and (close < level) and (wick / rng >= wick_ratio)


def nearest_level(price: float, levels: Sequence[float], direction: str) -> Optional[float]:
    """Next level in the trade's direction (above for long, below for short)."""
    if direction == "long":
        up = [L for L in levels if L > price]
        return min(up) if up else None
    dn = [L for L in levels if L < price]
    return max(dn) if dn else None


# ---- the confirmations the conviction gate counts ----------------------------------------
def factors(direction: str, open_: Sequence[float], high: Sequence[float], low: Sequence[float],
            close: Sequence[float], atr: float, key_levels: Sequence[float] = (), k: int = 3,
            htf_high: Sequence[float] = None, htf_low: Sequence[float] = None,
            level_tol_atr: float = 0.1) -> Dict[str, bool]:
    """Yes/no structure factors for a trade in `direction` decided at the close of the last bar:
      bos_aligned      the latest LTF break of structure is in the trade direction
      htf_structure    the HTF swing trend agrees (needs htf bars)
      ob_retest        price sits in a fresh order block of the trade direction
      fvg_support      price sits in an open fair value gap of the trade direction
      level_reaction   this bar rejected a key level in the trade direction
    """
    o, h, l, c = _arr(open_), _arr(high), _arr(low), _arr(close)
    f: Dict[str, bool] = {}
    if len(c) < 2 * k + 3 or atr <= 0:
        return {"bos_aligned": False, "htf_structure": False, "ob_retest": False, "fvg_support": False, "level_reaction": False}
    want = "up" if direction == "long" else "down"
    sw = swings(h, l, k)
    br = last_break(c, sw)
    f["bos_aligned"] = bool(br and br[0] == want)
    if htf_high is not None and htf_low is not None and len(htf_high) >= 2 * k + 3:
        f["htf_structure"] = trend_from_swings(swings(htf_high, htf_low, k)) == want
    else:
        f["htf_structure"] = False
    price = float(c[-1])
    tol = level_tol_atr * atr
    obs = order_blocks(o, h, l, c, atr)
    f["ob_retest"] = fresh_zone(obs, "bull_ob" if direction == "long" else "bear_ob", price, tol) is not None
    fvgs = fair_value_gaps(h, l, atr)
    f["fvg_support"] = fresh_zone(fvgs, "bull_fvg" if direction == "long" else "bear_fvg", price, tol) is not None
    f["level_reaction"] = any(rejection(float(o[-1]), float(h[-1]), float(l[-1]), float(c[-1]), L, direction, tol=tol)
                              for L in key_levels)
    return f
