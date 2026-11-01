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
}
DEFAULT = "growth"
CEILINGS = {"risk_mult": 2.0, "trades_mult": 2.5, "kelly_mult": 1.5}


def entries_paused(journal) -> bool:
    return (journal.get_state("entries_paused", "0") or "0") == "1"


def set_entries_paused(journal, paused: bool, note: str = "") -> None:
    journal.set_state("entries_paused", "1" if paused else "0")
    with journal.tx() as c:
        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', ?, ?)",
                  ("entries_paused" if paused else "entries_resumed", note))


def get_profile(journal) -> str:
    name = journal.get_state("risk_profile", DEFAULT) or DEFAULT
    return name if name in PROFILES else DEFAULT


def set_profile(journal, name: str, note: str = "") -> str:
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name}")
    journal.set_state("risk_profile", name)
    with journal.tx() as c:
        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', ?, ?)",
                  (f"risk_profile:{name}", note))
    return name


class EffectiveConfig:
    """cfg view with the active profile's multipliers applied within ceilings."""

    def __init__(self, cfg, profile: str, event_size_mult: float = 1.0):
        self._cfg = cfg
        p = PROFILES.get(profile, PROFILES[DEFAULT])
        self.max_pos_pct = min(cfg.max_pos_pct * p["risk_mult"] * event_size_mult, cfg.max_pos_pct * CEILINGS["risk_mult"], 0.2)
        self.max_trades_per_day = max(1, int(round(cfg.max_trades_per_day * p["trades_mult"])))
        self.kelly_fraction = min(cfg.kelly_fraction * p["kelly_mult"], 1.0)
        self.profile = profile
        self.event_size_mult = event_size_mult
        self._p = p

    def confluence_mult(self, score: int) -> float:
        """Size multiplier for a signal's confluence score (1 = base)."""
        return min(1.0 + self._p["confluence_step"] * max(0, int(score) - 1), self._p["confluence_max"])

    def __getattr__(self, k):
        return getattr(self._cfg, k)
