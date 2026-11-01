"""Pre-trade and session-level risk gates. Pure functions over a RiskState."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set


@dataclass
class RiskState:
    starting_equity: float
    equity: float
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    open_symbols: Set[str] = field(default_factory=set)
    halted: bool = False
    halt_reason: Optional[str] = None
    forced_dry_run: bool = False


def check_kill_switch(state: RiskState, kill_dd_pct: float) -> Optional[str]:
    """Drawdown from the session's starting equity, measured on TOTAL equity."""
    if state.starting_equity <= 0:
        return None
    dd = (state.starting_equity - state.equity) / state.starting_equity
    if dd >= kill_dd_pct:
        state.halted = True
        state.halt_reason = f"kill_switch dd={dd:.3%}"
        return state.halt_reason
    return None


def check_daily_loss(state: RiskState, max_daily_loss: float) -> Optional[str]:
    if state.realized_pnl_today <= -abs(max_daily_loss):
        state.forced_dry_run = True
        return f"daily_loss pnl={state.realized_pnl_today:.2f}"
    return None


def entry_gates(state: RiskState, *, symbol: str, direction: str, now: datetime,
                last_stop_iso: Optional[str], cfg) -> List[str]:
    """Return the list of reasons an entry is NOT allowed. Empty means allowed."""
    reasons: List[str] = []
    if state.halted:
        reasons.append("halted")
    # forced_dry_run is NOT a gate: the bot keeps journaling signals as DRY_RUN rows
    # after a daily-loss breach so the learning loop still sees them.
    if state.trades_today >= cfg.max_trades_per_day:
        reasons.append("max_trades_per_day")
    if len(state.open_symbols) >= cfg.max_positions:
        reasons.append("max_positions")
    if symbol in state.open_symbols:
        reasons.append("duplicate_position")
    if last_stop_iso:
        try:
            last = datetime.fromisoformat(last_stop_iso)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(minutes=cfg.stop_cooldown_min):
                reasons.append("stop_cooldown")
        except ValueError:
            pass
    return reasons


def past_cutoff(now_et: datetime, hhmm: str) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    return (now_et.hour, now_et.minute) >= (h, m)
