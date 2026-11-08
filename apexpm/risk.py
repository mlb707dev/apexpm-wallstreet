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


@dataclass
class BangerDecision:
    """Day-P&L defensive mode (owner's "banger rule", 2026-11-04). Once the day's gain reaches
    trigger_pct of the session-start equity the bot protects it: a floor at floor_pct of the peak gain
    halts the day if touched, size drops to size_mult, and only top-conviction setups may enter."""
    active: bool
    peak_gain: float
    halt: bool
    size_mult: float
    conviction_min: int
    floor: float


def banger_update(day_gain: float, start_equity: float, prev_peak: float, was_active: bool, *,
                  trigger_pct: float, floor_pct: float, size_mult: float, conviction_min: int) -> BangerDecision:
    """day_gain = realized + unrealized P&L today. Pure: the caller persists peak/active per session date."""
    if start_equity <= 0 or trigger_pct <= 0:
        return BangerDecision(False, prev_peak, False, 1.0, 0, 0.0)
    active = was_active or day_gain >= trigger_pct * start_equity
    if not active:
        return BangerDecision(False, max(prev_peak, day_gain), False, 1.0, 0, 0.0)
    peak = max(prev_peak, day_gain)
    floor = floor_pct * peak
    halt = day_gain <= floor
    return BangerDecision(True, peak, halt, size_mult, conviction_min, floor)
