from apexpm.db import Journal


def test_trade_roundtrip_and_pnl():
    j = Journal(":memory:")
    tid = j.open_trade(symbol="SPY", setup="example", direction="long", qty=10,
                       entry=100.0, stop=99.0, target=102.0, max_hold_min=90, mode="DRY_RUN")
    assert len(j.open_trades()) == 1
    j.close_trade(tid, exit_price=102.0, exit_reason="target", level_reached=1.5)
    t = j.get_trade(tid)
    assert t["pnl"] == 20.0
    assert t["rr_realized"] == 2.0
    assert j.open_trades() == []
    assert len(j.closed_trades("example")) == 1


def test_short_pnl_sign():
    j = Journal(":memory:")
    tid = j.open_trade(symbol="SPY", setup="example", direction="short", qty=5,
                       entry=100.0, stop=101.0, target=98.0, max_hold_min=1, mode="DRY_RUN")
    j.close_trade(tid, 101.0, "stop")
    assert j.get_trade(tid)["pnl"] == -5.0


def test_rejections_and_state():
    j = Journal(":memory:")
    j.reject("SPY", "example", "ribbon_ltf_disagrees")
    j.reject("SPY", "example", "ribbon_ltf_disagrees")
    j.reject("SPY", "example", "rr_too_low")
    from apexpm.db import utcnow
    hist = j.rejection_histogram(utcnow()[:10])
    assert hist == {"ribbon_ltf_disagrees": 2, "rr_too_low": 1}
    j.set_state("k", "v")
    assert j.get_state("k") == "v"
