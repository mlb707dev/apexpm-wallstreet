from apexpm.db import Journal
from apexpm.profiles import EffectiveConfig, PROFILES, get_profile, set_profile


def test_default_and_switch(cfg):
    j = Journal(":memory:")
    j._conn.executescript("CREATE TABLE IF NOT EXISTS strategy_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, name TEXT, event TEXT, detail TEXT)")
    assert get_profile(j) == "growth"  # loose by default
    set_profile(j, "conservative", "test")
    assert get_profile(j) == "conservative"
    ev = j.q("SELECT * FROM strategy_events")
    assert ev and ev[-1]["event"] == "risk_profile:conservative"


def test_effective_config_scales_within_ceilings(cfg):
    e = EffectiveConfig(cfg, "conservative")
    assert e.max_pos_pct == cfg.max_pos_pct * 0.5
    assert e.max_trades_per_day == max(1, round(cfg.max_trades_per_day * 0.5))
    g = EffectiveConfig(cfg, "growth")
    assert g.max_pos_pct == cfg.max_pos_pct * 1.5
    assert g.kelly_fraction <= 1.0
    # untouched attributes pass through
    assert g.kill_dd_pct == cfg.kill_dd_pct


def test_unknown_profile_rejected(cfg):
    j = Journal(":memory:")
    j._conn.executescript("CREATE TABLE IF NOT EXISTS strategy_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, name TEXT, event TEXT, detail TEXT)")
    import pytest
    with pytest.raises(ValueError):
        set_profile(j, "yolo")
    assert all(k in PROFILES for k in ("conservative", "standard", "growth"))
