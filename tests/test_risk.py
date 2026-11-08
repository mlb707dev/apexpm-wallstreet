from datetime import datetime, timedelta, timezone

from apexpm.risk import RiskState, check_daily_loss, check_kill_switch, entry_gates, past_cutoff


def test_kill_switch_on_total_equity():
    s = RiskState(starting_equity=100_000, equity=94_000)
    assert check_kill_switch(s, 0.05) is not None and s.halted


def test_kill_switch_not_tripped():
    s = RiskState(starting_equity=100_000, equity=99_000)
    assert check_kill_switch(s, 0.05) is None and not s.halted


def test_daily_loss_flips_dry_run():
    s = RiskState(starting_equity=1, equity=1, realized_pnl_today=-250)
    assert check_daily_loss(s, 200) is not None and s.forced_dry_run


def test_entry_gates(cfg):
    now = datetime.now(timezone.utc)
    s = RiskState(starting_equity=1, equity=1, trades_today=0, open_symbols={"QQQ"})
    assert entry_gates(s, symbol="SPY", direction="long", now=now, last_stop_iso=None, cfg=cfg) == []
    assert "duplicate_position" in entry_gates(s, symbol="QQQ", direction="long", now=now,
                                               last_stop_iso=None, cfg=cfg)
    s.trades_today = cfg.max_trades_per_day
    assert "max_trades_per_day" in entry_gates(s, symbol="SPY", direction="long", now=now,
                                               last_stop_iso=None, cfg=cfg)


def test_stop_cooldown(cfg):
    now = datetime.now(timezone.utc)
    s = RiskState(starting_equity=1, equity=1)
    recent = (now - timedelta(minutes=5)).isoformat()
    old = (now - timedelta(minutes=cfg.stop_cooldown_min + 1)).isoformat()
    assert "stop_cooldown" in entry_gates(s, symbol="SPY", direction="long", now=now,
                                          last_stop_iso=recent, cfg=cfg)
    assert entry_gates(s, symbol="SPY", direction="long", now=now, last_stop_iso=old, cfg=cfg) == []


def test_past_cutoff():
    assert past_cutoff(datetime(2026, 1, 1, 15, 31), "15:30")
    assert not past_cutoff(datetime(2026, 1, 1, 15, 29), "15:30")
