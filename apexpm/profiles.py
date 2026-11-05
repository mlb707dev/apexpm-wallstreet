"""Risk profiles: named multipliers the owner can switch at runtime.

Stored in the journal's state table (key 'risk_profile') and journaled as a
governance event. The loop applies them on every scan. Hard ceilings in config
still apply: a profile can only scale within them, never above them.
"""
from __future__ import annotations

from typing import Dict

PROFILES: Dict[str, Dict[str, float]] = {
    # risk_mult: risk per trade; trades_mult: max trades/day; kelly_mult: kelly fraction;
    # confluence_step: extra size per confluence point above 1 (capped by confluence_max)
    "conservative": {"risk_mult": 0.5, "trades_mult": 0.5, "kelly_mult": 0.5, "confluence_step": 0.0, "confluence_max": 1.0,
                     "blurb": "Half size, half the trades, no confluence boost. Tighten here in one tap."},
    "standard":     {"risk_mult": 1.0, "trades_mult": 1.0, "kelly_mult": 1.0, "confluence_step": 0.25, "confluence_max": 1.5,
                     "blurb": "The configured defaults. Stacked signals earn up to 1.5x size."},
    "growth":       {"risk_mult": 1.5, "trades_mult": 2.0, "kelly_mult": 1.25, "confluence_step": 0.5, "confluence_max": 2.0,
                     "blurb": "Loose: more size, more trades, stacked signals earn up to 2x. For fast growth; the hard ceilings still apply."},
    # Hyper growth: the small-account phase. Risk per trade is an absolute share of equity
    # (risk_abs), enough for one options contract at the premium stop. Two losses in a row
    # is a 75% drawdown at 0.5; the equity bands step down automatically as the account grows.
    "hyper":        {"risk_mult": 1.0, "risk_abs": 0.5, "trades_mult": 1.0, "kelly_mult": 1.0, "confluence_step": 0.0, "confluence_max": 1.0,
                     "blurb": "Hyper growth: up to 50% of equity at risk per trade, one position at a time. For the smallest accounts only."},
}
DEFAULT = "auto"       # choose by equity band
CEILINGS = {"risk_mult": 2.0, "trades_mult": 2.5, "kelly_mult": 1.5, "risk_abs": 1.0}
# Equity bands for the automatic profile (upper bound exclusive). Safeguards return by band.
BANDS = [(1000.0, "hyper"), (5000.0, "growth"), (10000.0, "growth"), (50000.0, "standard"), (float("inf"), "standard")]
# Per-band overrides applied on top of the profile: daily loss cap (fraction of session-start
# equity; 0 = off), max open positions, max trades per day.
BAND_RULES = [
    # risk_abs = max share of equity in one trade (owner, 2026-11-01: "as much as full up to $1k,
    # then scale back slightly as we grow to $5k"). In-trade stops still cap the loss per trade.
    (1000.0,        {"daily_loss_pct": 0.0,  "max_positions": 1, "max_trades_per_day": 5, "risk_abs": 1.0}),   # hyper: one at a time, in-trade stops only
    (5000.0,        {"daily_loss_pct": 0.15, "max_positions": 2, "max_trades_per_day": 5, "risk_abs": 0.75}),
    (10000.0,       {"daily_loss_pct": 0.10, "max_positions": 3, "max_trades_per_day": 5}),
    (50000.0,       {"daily_loss_pct": 0.08, "max_positions": 3, "max_trades_per_day": 6}),
    (float("inf"),  {"daily_loss_pct": 0.06, "max_positions": 3, "max_trades_per_day": 8}),
]
# Trades per day is a CAP, never a quota (owner, 2026-11-04): the bot never forces a trade to fill it.
# Owner's rule (2026-11-01): "usually 3 with exception, up to 5". A position is one
# symbol/contract; adding to it still counts as one. The exception is a one-tap
# switch on the dashboard that lifts the cap to MAX_POSITIONS_HARD for the day.
MAX_POSITIONS_HARD = 5


def positions_exception(journal) -> bool:
    """True when the owner lifted the cap to the hard ceiling for today (ET date stored with it)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    v = journal.get_state("positions_exception", "") or ""
    return v == datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def set_positions_exception(journal, on: bool, note: str = "") -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    journal.set_state("positions_exception", datetime.now(ZoneInfo("America/New_York")).date().isoformat() if on else "")
    with journal.tx() as c:
        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', ?, ?)",
                  ("positions_exception_on" if on else "positions_exception_off", note))


def band_rules(equity: float) -> dict:
    for upper, rules in BAND_RULES:
        if equity < upper:
            return rules
    return BAND_RULES[-1][1]


def band_profile(equity: float) -> str:
    for upper, name in BANDS:
        if equity < upper:
            return name
    return "standard"


def entries_paused(journal) -> bool:
    return (journal.get_state("entries_paused", "0") or "0") == "1"


def set_entries_paused(journal, paused: bool, note: str = "") -> None:
    journal.set_state("entries_paused", "1" if paused else "0")
    with journal.tx() as c:
        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', ?, ?)",
                  ("entries_paused" if paused else "entries_resumed", note))


def get_profile(journal, equity: float = None) -> str:
    """Manual override if set; otherwise the equity band ('auto')."""
    name = journal.get_state("risk_profile", DEFAULT) or DEFAULT
    if name in PROFILES:
        return name
    return band_profile(equity if equity is not None else 0.0)


def set_profile(journal, name: str, note: str = "") -> str:
    if name not in PROFILES and name != "auto":
        raise ValueError(f"unknown profile {name}")
    journal.set_state("risk_profile", name)
    with journal.tx() as c:
        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', ?, ?)",
                  (f"risk_profile:{name}", note))
    return name


class EffectiveConfig:
    """cfg view with the active profile's multipliers applied within ceilings."""

    def __init__(self, cfg, profile: str, event_size_mult: float = 1.0, equity: float = None,
                 positions_exception_on: bool = False):
        self._cfg = cfg
        p = PROFILES.get(profile, PROFILES["standard"])
        rules = band_rules(equity) if equity is not None else None
        if rules and rules.get("risk_abs"):          # equity band sets an absolute share (small-account phase)
            self.max_pos_pct = min(rules["risk_abs"] * event_size_mult, CEILINGS["risk_abs"])
        elif p.get("risk_abs"):                       # hyper profile chosen by hand, band unknown
            self.max_pos_pct = min(p["risk_abs"] * event_size_mult, CEILINGS["risk_abs"])
        else:
            self.max_pos_pct = min(cfg.max_pos_pct * p["risk_mult"] * event_size_mult, cfg.max_pos_pct * CEILINGS["risk_mult"], 0.2)
        self.max_trades_per_day = max(1, int(round(cfg.max_trades_per_day * p["trades_mult"])))
        self.kelly_fraction = min(cfg.kelly_fraction * p["kelly_mult"], 1.0)
        self.profile = profile
        self.event_size_mult = event_size_mult
        self._p = p
        self.max_positions = cfg.max_positions
        self.max_daily_loss_pct = cfg.max_daily_loss_pct
        if rules:
            self.max_positions = rules["max_positions"]
            self.max_trades_per_day = rules["max_trades_per_day"]
            self.max_daily_loss_pct = rules["daily_loss_pct"]
        if positions_exception_on:
            self.max_positions = max(self.max_positions, MAX_POSITIONS_HARD)

    def confluence_mult(self, score: int) -> float:
        """Size multiplier for a signal's confluence score (1 = base)."""
        return min(1.0 + self._p["confluence_step"] * max(0, int(score) - 1), self._p["confluence_max"])

    def __getattr__(self, k):
        return getattr(self._cfg, k)
