from apexpm.db import Journal
from apexpm.registry import Registry
from apexpm.strategies import ExampleBreakout


def _closed(j, setup, rr, reason="target"):
    tid = j.open_trade(symbol="SPY", setup=setup, direction="long", qty=1, entry=100, stop=99,
                       target=100 + rr if rr > 0 else 102, max_hold_min=1, mode="PAPER", level_targeted=1.5)
    j.close_trade(tid, 100 + rr, reason)


def test_status_persists_and_auto_suspends(cfg):
    j = Journal(":memory:")
    r = Registry(j, cfg)
    r.register(ExampleBreakout(), "paper")
    assert [s.name for s in r.active()] == ["example_breakout"]
    cfg.review_min_trades = 5
    for _ in range(6):
        _closed(j, "example_breakout", -1.0, reason="stop")
    assert r.review() and r.status("example_breakout") == "suspended"
    for _ in range(10):
        _closed(j, "example_breakout", 2.0)
    assert r.review() == [] and r.status("example_breakout") == "suspended"   # never un-suspends itself
