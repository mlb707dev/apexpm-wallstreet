"""Session structure (the ICT-style session boxes on the owner's chart, 2026-11-07), for US equities:

  pre-market     04:00-09:29 ET: its high and low are levels the open reacts to
  opening range  09:30-09:59 ET (the first OR_MINUTES): its high and low frame the morning
  session name   premarket / am (09:30-11:30) / midday (11:30-13:30) / pm (13:30-16:00) / closed

All from the 5-minute bars the loop and the backtester already hold; nothing is fetched. Bars are
expected with a timezone-aware index (UTC); the owner's bars from Alpaca are.
"""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Dict, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OR_MINUTES = 30
PM_START, OPEN, OR_END = time(4, 0), time(9, 30), time(10, 0)
AM_END, MID_END, CLOSE = time(11, 30), time(13, 30), time(16, 0)


def session_name(now_et: datetime) -> str:
    t = now_et.time()
    if t < OPEN:
        return "premarket" if t >= PM_START else "closed"
    if t < AM_END:
        return "am"
    if t < MID_END:
        return "midday"
    if t < CLOSE:
        return "pm"
    return "closed"


def rth(bars):
    """Regular-hours bars only (09:30-16:00 ET), the same set the backtest history holds. Alpaca's live
    bars include pre-market and after-hours; computing the ribbon, confirmations and structure on those
    would judge live signals on different bars than the strategies were tested on (found 2026-11-07).
    Frames without a datetime index (test fakes) pass through unchanged."""
    if bars is None or len(bars) == 0 or not hasattr(bars.index, "tz_convert"):
        return bars
    idx = bars.index.tz_convert(ET)
    mins = idx.hour * 60 + idx.minute
    return bars[(mins >= 9 * 60 + 30) & (mins < 16 * 60)]


def levels_for_day(bars, day: date, now_et: Optional[datetime] = None) -> Dict[str, float]:
    """pm_high/pm_low from pre-market bars of `day`, or_high/or_low once the opening range is complete.
    Zeros when the bars do not cover that window (regular-hours-only history has no pre-market)."""
    out = {"pm_high": 0.0, "pm_low": 0.0, "or_high": 0.0, "or_low": 0.0}
    if bars is None or len(bars) == 0 or not hasattr(bars.index, "tz_convert"):
        return out
    idx = bars.index.tz_convert(ET)
    same = idx.date == day
    if not same.any():
        return out
    times = idx.time
    pm = bars[same & (times >= PM_START) & (times < OPEN)]
    if len(pm):
        out["pm_high"] = float(pm["high"].max() if "high" in pm else pm["close"].max())
        out["pm_low"] = float(pm["low"].min() if "low" in pm else pm["close"].min())
    orng = bars[same & (times >= OPEN) & (times < OR_END)]
    # the opening range is a level only once it is complete (otherwise it is just "the high so far")
    complete = now_et is None or now_et.time() >= OR_END
    if len(orng) and complete:
        out["or_high"] = float(orng["high"].max() if "high" in orng else orng["close"].max())
        out["or_low"] = float(orng["low"].min() if "low" in orng else orng["close"].min())
    return out


def levels_by_day(bars) -> Dict[date, Dict[str, float]]:
    """All days at once (the backtester): one timezone conversion for the whole history, then a groupby.
    Same values as levels_for_day(bars, day) for a completed day."""
    out: Dict[date, Dict[str, float]] = {}
    if bars is None or len(bars) == 0 or not hasattr(bars.index, "tz_convert"):
        return out
    import pandas as pd
    idx = bars.index.tz_convert(ET)
    hi = bars["high"] if "high" in bars else bars["close"]
    lo = bars["low"] if "low" in bars else bars["close"]
    t = idx.time
    days = pd.Series(idx.date, index=bars.index)
    pm_mask = (t >= PM_START) & (t < OPEN)
    or_mask = (t >= OPEN) & (t < OR_END)
    pm_hi = hi[pm_mask].groupby(days[pm_mask]).max()
    pm_lo = lo[pm_mask].groupby(days[pm_mask]).min()
    or_hi = hi[or_mask].groupby(days[or_mask]).max()
    or_lo = lo[or_mask].groupby(days[or_mask]).min()
    for d in set(days.values):
        out[d] = {"pm_high": float(pm_hi.get(d, 0.0)), "pm_low": float(pm_lo.get(d, 0.0)),
                  "or_high": float(or_hi.get(d, 0.0)), "or_low": float(or_lo.get(d, 0.0))}
    return out


def window_ok(session: str, want: Optional[str]) -> bool:
    """Session filter for a strategy setting."""
    if not want or want == "any":
        return True
    if want == "am_pm":
        return session in ("am", "pm")
    return session == want
