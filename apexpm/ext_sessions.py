"""Extended-hours session clock for US equities on Alpaca (24/5), shared by the overnight backtester,
the overnight desk in the loop, the history fetch and the postmortem.

  pre        04:00-09:30 ET, Mon-Fri
  rth        09:30-16:00 ET (13:00 on NYSE early-close days)
  ah         16:00-20:00 ET (13:00-17:00 on early-close days)
  overnight  20:00-04:00 ET, Sunday 20:00 through Friday 04:00 (Alpaca / Blue Ocean ATS)
  closed     everything else (Friday 20:00 to Sunday 20:00, 17:00-20:00 on early-close days)

Exchange holidays are not listed: a holiday simply has no regular-hours bars, and the "anchor" of an
extended-hours bar is always the most recent regular session that actually traded (see anchor_index).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
PRE_START, OPEN, CLOSE, AH_END = time(4, 0), time(9, 30), time(16, 0), time(20, 0)
EARLY_CLOSE_T, EARLY_AH_END = time(13, 0), time(17, 0)
SESSIONS = ("pre", "rth", "ah", "overnight")
EXT_SESSIONS = ("pre", "ah", "overnight")

# NYSE early closes (13:00 ET). Past years from the NYSE calendar; 2026 matches apexpm.calendar.EARLY_CLOSE.
EARLY_CLOSE_DATES = {
    "2021-11-26",
    "2022-11-25",
    "2023-07-03", "2023-11-24",
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
}


def is_early_close(d: date) -> bool:
    return d.isoformat() in EARLY_CLOSE_DATES


def close_time(d: date) -> time:
    return EARLY_CLOSE_T if is_early_close(d) else CLOSE


def session_at(now_et: datetime) -> str:
    """Session name for one ET datetime."""
    t, wd = now_et.time(), now_et.weekday()          # Mon=0 .. Sun=6
    early = is_early_close(now_et.date())
    if t >= AH_END:
        return "overnight" if wd in (6, 0, 1, 2, 3) else "closed"       # Sun-Thu evenings
    if t < PRE_START:
        return "overnight" if wd in (0, 1, 2, 3, 4) else "closed"       # Mon-Fri early mornings
    if wd >= 5:
        return "closed"
    if t < OPEN:
        return "pre"
    if t < (EARLY_CLOSE_T if early else CLOSE):
        return "rth"
    if early:
        return "ah" if t < EARLY_AH_END else "closed"
    return "ah"


def session_of_index(idx: pd.DatetimeIndex) -> np.ndarray:
    """Vectorised session_at for a UTC (tz-aware) index."""
    et = idx.tz_convert(ET)
    mins = np.asarray(et.hour * 60 + et.minute)
    wd = np.asarray(et.weekday)
    days = np.asarray([d.isoformat() for d in et.date])
    early = np.isin(days, list(EARLY_CLOSE_DATES))
    out = np.full(len(idx), "closed", dtype=object)
    late = mins >= 20 * 60
    early_morn = mins < 4 * 60
    out[late & np.isin(wd, [6, 0, 1, 2, 3])] = "overnight"
    out[early_morn & np.isin(wd, [0, 1, 2, 3, 4])] = "overnight"
    day = (~late) & (~early_morn) & (wd < 5)
    out[day & (mins < 570)] = "pre"
    close_m = np.where(early, 13 * 60, 16 * 60)
    out[day & (mins >= 570) & (mins < close_m)] = "rth"
    ah_end = np.where(early, 17 * 60, 20 * 60)
    out[day & (mins >= close_m) & (mins < ah_end)] = "ah"
    return out


def session_start(now_et: datetime, session: str) -> datetime:
    """ET datetime the given session began (for the session containing now_et)."""
    d = now_et.date()
    if session == "pre":
        return datetime.combine(d, PRE_START, ET)
    if session == "rth":
        return datetime.combine(d, OPEN, ET)
    if session == "ah":
        return datetime.combine(d, close_time(d), ET)
    if session == "overnight":
        if now_et.time() >= AH_END:
            return datetime.combine(d, AH_END, ET)
        return datetime.combine(d - timedelta(days=1), AH_END, ET)
    return now_et


def session_end(now_et: datetime, session: str) -> datetime:
    d = now_et.date()
    if session == "pre":
        return datetime.combine(d, OPEN, ET)
    if session == "rth":
        return datetime.combine(d, close_time(d), ET)
    if session == "ah":
        return datetime.combine(d, EARLY_AH_END if is_early_close(d) else AH_END, ET)
    if session == "overnight":
        if now_et.time() >= AH_END:
            return datetime.combine(d + timedelta(days=1), PRE_START, ET)
        return datetime.combine(d, PRE_START, ET)
    return now_et


def next_weekday(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def resolve_exit_by(rule: str, now_et: datetime, next_open_et: Optional[datetime] = None) -> datetime:
    """Turn a strategy's hold rule into an ET deadline.
      session_end   end of the current extended session (AH 20:00, pre 09:30 less 2 minutes)
      ah_end        19:55 today (after-hours)
      pre_end       09:25 on the next trading morning (or today when already pre-market)
      open_<N>      N minutes after the next regular open (open_0 = at the open)
    next_open_et: the next regular open when known (the backtester knows holidays from the data);
    otherwise the next weekday 09:30 is assumed."""
    def nxt_open() -> datetime:
        if next_open_et is not None:
            return next_open_et
        if now_et.time() < OPEN and now_et.weekday() < 5:
            return datetime.combine(now_et.date(), OPEN, ET)
        return datetime.combine(next_weekday(now_et.date()), OPEN, ET)
    if rule == "ah_end":
        return datetime.combine(now_et.date(), time(19, 55), ET)
    if rule == "pre_end":
        return nxt_open() - timedelta(minutes=5)
    if rule.startswith("open_"):
        return nxt_open() + timedelta(minutes=int(rule.split("_", 1)[1]))
    if rule == "session_end":
        s = session_at(now_et)
        return session_end(now_et, s) - timedelta(minutes=5)
    raise ValueError(f"unknown exit rule {rule!r}")


def rth_days(bars: pd.DataFrame) -> pd.DataFrame:
    """Per regular session (index = ET date): open, high, low, close, volume and close_ts (UTC), from
    the rth-tagged bars of an extended file."""
    r = bars[bars["session"] == "rth"]
    if r.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])
    d = pd.Series(r.index.tz_convert(ET).date, index=r.index)
    g = r.groupby(d.values)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum()})
    out["close_ts"] = [pd.Timestamp(datetime.combine(x, close_time(x), ET)).tz_convert("UTC") for x in out.index]
    out["open_ts"] = [pd.Timestamp(datetime.combine(x, OPEN, ET)).tz_convert("UTC") for x in out.index]
    return out


def anchor_index(ts_utc: Sequence, close_ts: Sequence) -> np.ndarray:
    """For each bar time, the position in close_ts (sorted regular-session closes) of the latest session that
    had closed at that time (-1 when none). An extended bar's anchor is that session: its close is the
    'prior close' every overnight strategy measures from."""
    a = np.asarray(pd.DatetimeIndex(ts_utc).asi8)
    c = np.asarray(pd.DatetimeIndex(close_ts).asi8)
    return np.searchsorted(c, a, side="right") - 1


def wilder_atr(daily: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR on regular-session OHLC (value on a date includes that date)."""
    h, l, c = daily["high"].values, daily["low"].values, daily["close"].values
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.nanmax(np.vstack([h - l, np.abs(h - pc), np.abs(l - pc)]), axis=0)
    atr = np.full(len(tr), np.nan)
    if len(tr) >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(atr, index=daily.index)


def eligible_sessions(attributes: Optional[List[str]]) -> Dict[str, bool]:
    """Which extended sessions an Alpaca asset can trade in, from Asset.attributes (alpaca-py gives a plain
    list of strings). Pre-market and after-hours are open to every NMS stock; the overnight session needs
    'overnight_tradable' and is closed while 'overnight_halted' is set."""
    a = set(attributes or [])
    return {"pre": True, "ah": True, "rth": True,
            "overnight": "overnight_tradable" in a and "overnight_halted" not in a}
