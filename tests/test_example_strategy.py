from apexpm.levels import build_levels
from apexpm.ribbon import Ribbon
from apexpm.strategies import ExampleBreakout
from apexpm.strategies.base import MarketSnapshot

BULL, BEAR = Ribbon(3, 2, 1), Ribbon(1, 2, 3)


def snap(price, htf=BULL, reached=None):
    lv = build_levels(anchor=100.0, atr=4.0)
    return MarketSnapshot(symbol="SPY", price=price, levels=lv, ribbon_ltf=BULL, ribbon_htf=htf,
                          ltf_closes=[], reached_today=reached or {})


def test_long_breakout(cfg):
    sig, why = ExampleBreakout().evaluate(snap(104.2), cfg)
    assert why is None and sig.direction == "long"
    assert sig.entry == 104.0 and sig.target == 106.0 and sig.stop == 102.0


def test_rejections(cfg):
    assert ExampleBreakout().evaluate(snap(101.0), cfg)[1] == "not_at_entry_level"
    assert ExampleBreakout().evaluate(snap(104.2, htf=BEAR), cfg)[1] == "ribbon_htf_disagrees"
    assert ExampleBreakout().evaluate(snap(104.2, reached={"long": 1.6}), cfg)[1] == "target_already_hit_today"
