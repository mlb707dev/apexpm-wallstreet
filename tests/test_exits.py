from apexpm.exits import ExitPlan, update


class C:
    exit_be_at_r = 1.0
    exit_be_offset_atr = 0.02
    exit_runner = True
    exit_trail_atr = 0.1


def plan():
    # long from 100, stop 98 (risk 2), target 104, atr 4, levels up: 104, 106, 108
    return ExitPlan("long", 100.0, 98.0, 98.0, 104.0, 4.0, [102.0, 104.0, 106.0, 108.0])


def test_breakeven_moves_stop_once():
    p = plan()
    r, px, changed = update(p, hi=102.5, lo=101.0, momentum=False, cfg=C)
    assert r is None and changed and p.stop == 100.08 and p.be_done


def test_target_without_momentum_takes_profit():
    p = plan()
    r, px, _ = update(p, hi=104.2, lo=103.0, momentum=False, cfg=C)
    assert r == "target" and px == 104.0


def test_runner_trails_up_the_levels():
    p = plan()
    r, px, changed = update(p, hi=104.2, lo=103.7, momentum=True, cfg=C)
    assert r is None and p.runner and p.target == 106.0 and p.stop == 104.0 - 0.4
    r, px, _ = update(p, hi=106.5, lo=105.7, momentum=True, cfg=C)
    assert r is None and p.target == 108.0 and p.stop == 106.0 - 0.4
    r, px, _ = update(p, hi=106.0, lo=105.5, momentum=True, cfg=C)
    assert r == "trail_stop" and px == 105.6


def test_runner_without_next_level_takes_profit():
    p = ExitPlan("long", 100.0, 98.0, 98.0, 108.0, 4.0, [104.0, 106.0, 108.0])
    r, px, _ = update(p, hi=108.1, lo=107.0, momentum=True, cfg=C)
    assert r == "target" and px == 108.0


def test_stop_checked_before_target_same_bar():
    p = plan()
    r, px, _ = update(p, hi=104.5, lo=97.5, momentum=True, cfg=C)
    assert r == "stop" and px == 98.0


def test_short_mirror():
    p = ExitPlan("short", 100.0, 102.0, 102.0, 96.0, 4.0, [98.0, 96.0, 94.0])
    r, px, _ = update(p, hi=96.3, lo=95.8, momentum=True, cfg=C)
    assert r is None and p.runner and p.target == 94.0 and p.stop == 96.4


def test_same_bar_through_ratcheted_stop_is_a_stop():
    p = plan()
    # target touched (runner would start at stop 103.6) but the bar also printed 103.0 → conservative stop
    r, px, _ = update(p, hi=104.2, lo=103.0, momentum=True, cfg=C)
    assert r == "trail_stop" and px == 104.0 - 0.4
