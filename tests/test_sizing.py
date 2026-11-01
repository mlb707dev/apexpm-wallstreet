import pytest

from apexpm.sizing import kelly_fraction_full, rolling_stats, size_trade, vol_scale


def test_kelly_formula():
    assert kelly_fraction_full(0.6, 1.0) == pytest.approx(0.2)
    assert kelly_fraction_full(0.5, 1.0) == pytest.approx(0.0)
    assert kelly_fraction_full(0.6, 0) == 0.0


def test_rolling_stats():
    assert rolling_stats([10, -5, 10, -5]) == (0.5, 2.0)
    assert rolling_stats([1, 2]) is None


def test_vol_scale_clips():
    assert vol_scale(atr=1.0, price=100.0, target_daily_vol=0.01) == pytest.approx(1.0)
    assert vol_scale(atr=10.0, price=100.0, target_daily_vol=0.01) == 0.25
    assert vol_scale(atr=0.1, price=100.0, target_daily_vol=0.01) == 1.5


def test_whole_shares_use_priors_then_caps(cfg):
    cfg.fractional_shares = False
    r = size_trade(equity=100_000, price=100.0, entry=100.0, stop=99.0, target=102.0,
                   atr=2.0, closed_pnls=[], cfg=cfg)
    assert r.qty > 0 and float(r.qty).is_integer()
    assert r.kelly_used <= cfg.max_pos_pct
    assert r.notional <= cfg.max_pos_dollars + 100.0


def test_fractional_shares_for_small_accounts(cfg):
    cfg.fractional_shares = True
    r = size_trade(equity=1_000, price=400.0, entry=400.0, stop=398.0, target=404.0,
                   atr=4.0, closed_pnls=[], cfg=cfg)
    assert 0 < r.qty < 10
    assert r.notional <= 1_000 * cfg.max_notional_pct + 1e-6


def test_negative_edge_and_zero_risk(cfg):
    cfg.prior_win_rate = 0.3
    r = size_trade(equity=100_000, price=100, entry=100, stop=99, target=101, atr=2.0, closed_pnls=[], cfg=cfg)
    assert r.qty == 0 and r.reason == "negative_edge"
    r = size_trade(equity=1, price=100, entry=100, stop=100, target=101, atr=1, closed_pnls=[], cfg=cfg)
    assert r.reason == "zero_risk_per_share"
