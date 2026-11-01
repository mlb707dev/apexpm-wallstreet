import numpy as np

from apexpm.ribbon import Ribbon, folded, ribbon_from_closes


def test_rising_series_is_bull():
    closes = np.linspace(100, 120, 60)
    r = ribbon_from_closes(closes)
    assert r.state == "bull"
    assert r.agrees("long") and not r.agrees("short")


def test_falling_series_is_bear():
    closes = np.linspace(120, 100, 60)
    assert ribbon_from_closes(closes).state == "bear"


def test_flat_series_is_mixed():
    r = Ribbon(fast=100.0, mid=100.0, slow=100.0)
    assert r.state == "mixed"
    assert not r.agrees("long")


def test_fold_detection():
    assert folded(Ribbon(1, 2, 3), Ribbon(3, 2, 1))
    assert not folded(Ribbon(3, 2, 1), Ribbon(3, 2, 1))
    assert not folded(Ribbon(3, 2, 1), Ribbon(2, 2, 2))  # into mixed is not a fold
