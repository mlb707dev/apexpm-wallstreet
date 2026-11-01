import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402

from apexpm.config import Config  # noqa: E402


@pytest.fixture
def cfg(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ALPACA_", "GG_", "MAX_", "KELLY", "PAPER", "DRY_RUN")):
            monkeypatch.delenv(k, raising=False)
    c = Config()
    c.conviction_min = 0     # synthetic fixtures; the gate has its own tests
    return c
