import numpy as np

from apexpm.conviction import indicators, score
from apexpm.levels import build_levels
from apexpm.ribbon import Ribbon
from apexpm.strategies.base import MarketSnapshot

BULL, BEAR = Ribbon(3, 2, 1), Ribbon(1, 2, 3)


def snap(price, **kw):
    lv = build_levels(anchor=100.0, atr=4.0)
    base = dict(symbol="SPY", price=price, levels=lv, ribbon_ltf=BULL, ribbon_htf=BULL, ltf_closes=[], reached_today={},
                bar_open=price - 0.3, bar_high=price + 0.1, bar_low=price - 0.4, bar_volume=2000, vol_avg20=1000)
    base.update(kw)
    return MarketSnapshot(**base)


def test_indicators_shape():
    ind = indicators(np.linspace(100, 110, 60))
    assert {"ema_fast", "ema_mid", "macd_hist", "rsi", "stoch_k", "stoch_d"} <= set(ind.columns)
    assert ind["rsi"].iloc[-1] > 50


def test_breakout_with_volume_scores_high():
    # dip then a sharp turn up through the first upside band on the last bar (band value taken from the level map)
    L = min(build_levels(anchor=100.0, atr=4.0).up.values())
    closes = list(np.linspace(L - 0.1, L - 1.6, 40)) + list(np.linspace(L - 1.5, L + 0.2, 10))
    c = score("long", snap(L + 0.2, bar_open=L - 0.1, bar_low=L - 0.15, bar_high=L + 0.25), closes)
    assert c.factors["level_break_volume"] and c.factors["htf_stack"] and c.factors["anchor_side"]
    assert c.score >= 4


def test_no_confirmation_scores_low():
    closes = list(np.linspace(100, 100.2, 60))     # drift, no cross, no level
    c = score("long", snap(100.2, bar_open=100.19, bar_low=100.18, bar_high=100.21, bar_volume=500, ribbon_htf=BEAR), closes)
    assert c.score <= 1


def test_short_mirror():
    L = max(build_levels(anchor=100.0, atr=4.0).down.values())
    closes = list(np.linspace(L + 0.1, L + 1.6, 40)) + list(np.linspace(L + 1.5, L - 0.2, 10))
    c = score("short", snap(L - 0.2, bar_open=L + 0.1, bar_high=L + 0.15, bar_low=L - 0.25, ribbon_ltf=BEAR, ribbon_htf=BEAR), closes)
    assert c.factors["htf_stack"] and c.factors["anchor_side"] and c.factors["level_break_volume"]
