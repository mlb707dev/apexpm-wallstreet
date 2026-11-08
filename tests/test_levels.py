import numpy as np
import pytest

from apexpm.levels import build_levels, wilder_atr, wilder_atr_series


def test_wilder_atr_constant_range():
    assert wilder_atr([101.0] * 30, [99.0] * 30, [100.0] * 30, 14) == pytest.approx(2.0)


def test_levels_symmetry_and_anchor():
    lv = build_levels(anchor=100.0, atr=4.0)
    assert lv.up[1.0] == 104.0 and lv.down[1.0] == 96.0
    assert lv.level(0, "long") == 100.0
    assert lv.level(0.75, "short") == pytest.approx(97.0)
    assert lv.position_in_range(102.0) == pytest.approx(0.5)


def test_atr_series_matches_scalar():
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, 80))
    high = close + rng.uniform(0.2, 1.5, 80)
    low = close - rng.uniform(0.2, 1.5, 80)
    series = wilder_atr_series(high, low, close, 14)
    for end in (15, 40, 80):
        assert series[end - 1] == pytest.approx(wilder_atr(high[:end], low[:end], close[:end], 14))
