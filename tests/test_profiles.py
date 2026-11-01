from apexpm.db import Journal
from apexpm.profiles import EffectiveConfig, PROFILES, get_profile, set_profile


def test_default_and_switch(cfg):
    j = Journal(":memory:")
    j._conn.executescript("CREATE TABLE IF NOT EXISTS strategy_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, name TEXT, event TEXT, detail TEXT)")
    assert get_profile(j, 500.0) == "hyper" and get_profile(j, 5000.0) == "growth" and get_profile(j, 50000.0) == "standard"
    from apexpm.profiles import band_rules
    assert band_rules(500.0)["daily_loss_pct"] == 0.0 and band_rules(500.0)["max_positions"] == 1
    assert band_rules(2000.0)["daily_loss_pct"] == 0.15 and band_rules(20000.0)["daily_loss_pct"] == 0.08
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


def test_hyper_sizes_to_half_equity(cfg):
    from apexpm.sizing import size_trade
    e = EffectiveConfig(cfg, "hyper")
    assert e.max_pos_pct == 0.5                       # profile alone (no equity band known)
    assert EffectiveConfig(cfg, "hyper", 1.0, 800).max_pos_pct == 1.0     # under $1k: full equity allowed
    assert EffectiveConfig(cfg, "growth", 1.0, 3000).max_pos_pct == 0.75  # $1k–5k: scaled back slightly
    assert EffectiveConfig(cfg, "growth", 1.0, 7000).max_pos_pct == cfg.max_pos_pct * 1.5
    cfg.fractional_shares = True
    r = size_trade(equity=500, price=100.0, entry=100.0, stop=98.0, target=104.0, atr=2.0, closed_pnls=[], cfg=e)
    # half the equity at risk / $2 per share, capped by the notional ceiling
    assert r.qty > 0 and r.notional <= 500 * cfg.max_notional_pct + 1e-6
