"""Market calendar: known event days and the policy the bot applies on them.

Rule-based events are computed (OPEX, quad witching, NFP, month end). FOMC
dates are a yearly table (verify each January; override with FOMC_DATES).
Earnings come from the journal's `calendar_events` table, refreshed by
scripts/refresh_calendar.py for a watchlist of market movers.

Policy per event type (all configurable in POLICY):
  size_mult        multiply risk per trade for the whole session
  no_entry         (start, end) ET windows with no new entries
  skip_symbols     symbols not traded that day (earnings of the symbol itself)
"""
from __future__ import annotations

import calendar as _cal
import json
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple

# FOMC decision days (second day of each two-day meeting). VERIFY EACH YEAR.
FOMC = {
    2026: ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"],
}

# Names whose earnings move the whole tape. Earnings dates are fetched, not hardcoded.
MARKET_MOVERS = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD", "AVGO", "NFLX", "JPM"]

POLICY: Dict[str, Dict] = {
    "fomc":          {"size_mult": 0.5,  "no_entry": [("13:30", "15:00")], "label": "FOMC decision 2:00 ET"},
    "quad_witching": {"size_mult": 0.5,  "no_entry": [("15:00", "16:00")], "label": "Quad witching"},
    "opex":          {"size_mult": 0.75, "no_entry": [("15:15", "16:00")], "label": "Monthly OPEX"},
    "nfp":           {"size_mult": 0.75, "no_entry": [("09:30", "09:45")], "label": "Jobs report 8:30 ET"},
    "cpi":           {"size_mult": 0.75, "no_entry": [("09:30", "09:45")], "label": "CPI 8:30 ET"},
    "month_end":     {"size_mult": 1.0,  "no_entry": [("15:30", "16:00")], "label": "Month-end rebalancing"},
    "earnings_mover":{"size_mult": 0.75, "no_entry": [],                   "label": "Market-mover earnings"},
    "earnings_self": {"size_mult": 0.0,  "no_entry": [],                   "label": "Earnings of a traded symbol"},
    "half_day":      {"size_mult": 0.5,  "no_entry": [("12:30", "13:00")], "label": "Early close 1:00 ET"},
}

EARLY_CLOSE = {  # NYSE early closes (1:00 ET). VERIFY EACH YEAR.
    2026: ["2026-11-27", "2026-12-24"],
}


def third_friday(y: int, m: int) -> date:
    c = _cal.Calendar(firstweekday=0)
    fridays = [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4]
    return fridays[2]


def first_friday(y: int, m: int) -> date:
    c = _cal.Calendar(firstweekday=0)
    return [d for d in c.itermonthdates(y, m) if d.month == m and d.weekday() == 4][0]


def last_trading_day(y: int, m: int) -> date:
    d = date(y, m, _cal.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fomc_dates(year: int) -> List[date]:
    env = os.getenv("FOMC_DATES")
    if env:
        return [date.fromisoformat(x.strip()) for x in env.split(",") if x.strip()]
    return [date.fromisoformat(x) for x in FOMC.get(year, [])]


@dataclass
class DayContext:
    date: date
    events: List[Dict] = field(default_factory=list)   # {"type","label","symbol"?}
    size_mult: float = 1.0
    no_entry: List[Tuple[str, str]] = field(default_factory=list)
    skip_symbols: Set[str] = field(default_factory=set)

    def entries_blocked_at(self, hhmm: str) -> Optional[str]:
        for a, b in self.no_entry:
            if a <= hhmm < b:
                return f"event_window:{a}-{b}"
        return None

    def as_dict(self) -> Dict:
        return {"date": self.date.isoformat(), "events": self.events, "size_mult": self.size_mult,
                "no_entry": self.no_entry, "skip_symbols": sorted(self.skip_symbols)}


def rule_events(d: date) -> List[Dict]:
    ev = []
    if d in fomc_dates(d.year):
        ev.append({"type": "fomc"})
    tf = third_friday(d.year, d.month)
    if d == tf:
        ev.append({"type": "quad_witching" if d.month in (3, 6, 9, 12) else "opex"})
    if d == first_friday(d.year, d.month) and d.day <= 7:
        ev.append({"type": "nfp"})
    if d == last_trading_day(d.year, d.month):
        ev.append({"type": "month_end"})
    if d.isoformat() in EARLY_CLOSE.get(d.year, []):
        ev.append({"type": "half_day"})
    for e in ev:
        e["label"] = POLICY[e["type"]]["label"]
    return ev


def earnings_events(journal, d: date, traded: List[str]) -> List[Dict]:
    """Earnings on `d` (or after the close on the prior session) from the journal table."""
    if journal is None:
        return []
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    try:
        rows = journal.q("SELECT symbol, date, timing FROM calendar_events WHERE kind='earnings' AND (date=? OR date=?)",
                         (d.isoformat(), prev.isoformat()))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        on_day = (r["date"] == d.isoformat() and (r["timing"] or "") != "amc") or \
                 (r["date"] != d.isoformat() and (r["timing"] or "") == "amc")
        if not on_day:
            continue
        kind = "earnings_self" if r["symbol"] in traded else "earnings_mover"
        out.append({"type": kind, "symbol": r["symbol"], "label": f"{r['symbol']} earnings ({r['timing'] or 'tbd'})"})
    return out


def day_context(d: date, traded: List[str], journal=None) -> DayContext:
    ctx = DayContext(date=d)
    ctx.events = rule_events(d) + earnings_events(journal, d, traded)
    for e in ctx.events:
        p = POLICY[e["type"]]
        ctx.size_mult = min(ctx.size_mult, p["size_mult"])
        ctx.no_entry += list(p["no_entry"])
        if e["type"] == "earnings_self":
            ctx.skip_symbols.add(e["symbol"])
    if ctx.size_mult == 0.0:            # earnings of a traded symbol → skip it, others at 0.75
        ctx.size_mult = 0.75
    return ctx


def upcoming(journal, start: date, days: int = 21, traded: Optional[List[str]] = None) -> List[Dict]:
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ctx = day_context(d, traded or [], journal)
        for e in ctx.events:
            out.append({"date": d.isoformat(), **e, "size_mult": POLICY[e["type"]]["size_mult"],
                        "no_entry": POLICY[e["type"]]["no_entry"]})
    return out


def ensure_table(journal) -> None:
    with journal.tx() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS calendar_events (
            kind TEXT NOT NULL, symbol TEXT, date TEXT NOT NULL, timing TEXT, source TEXT, fetched_at TEXT,
            PRIMARY KEY (kind, symbol, date))""")


BIG = ("fomc", "quad_witching", "opex", "cpi", "nfp")


def alerts(journal, now_et, traded: List[str], horizon_days: int = 2) -> List[Dict]:
    """Events today and within the next `horizon_days` trading days, with urgency.
    severity: 'today' | 'tomorrow' | 'soon'. flash=True from noon the day before OPEX/witching."""
    out = []
    d0 = now_et.date()
    checked = 0
    d = d0
    while checked <= horizon_days:
        if d.weekday() < 5:
            ctx = day_context(d, traded, journal)
            for e in ctx.events:
                days = (d - d0).days
                sev = "today" if days == 0 else "tomorrow" if days == 1 else "soon"
                flash = e["type"] in ("opex", "quad_witching", "fomc") and days == 1 and now_et.hour >= 12
                out.append({"date": d.isoformat(), "days": days, "type": e["type"], "label": e["label"],
                            "symbol": e.get("symbol"), "severity": sev, "flash": flash, "big": e["type"] in BIG,
                            "policy": POLICY[e["type"]]})
            checked += 1
        d += timedelta(days=1)
    out.sort(key=lambda a: (a["days"], not a["big"]))
    return out
