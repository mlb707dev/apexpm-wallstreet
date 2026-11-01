from datetime import date, timedelta

from apexpm.db import Journal
from apexpm.options import Contract, contracts_for_risk, pick_contract
from apexpm.webhook import WebhookStore, validate


def chain(today):
    exp = today + timedelta(days=2)
    rows = []
    for k in (500, 502, 504, 506, 508):
        rows.append(Contract("SPY", k, exp, "call", bid=3.0 - (k - 500) * 0.4, ask=3.1 - (k - 500) * 0.4, open_interest=5000))
        rows.append(Contract("SPY", k, exp, "put", bid=1.0 + (k - 500) * 0.4, ask=1.1 + (k - 500) * 0.4, open_interest=5000))
    rows.append(Contract("SPY", 503, exp, "call", bid=1.0, ask=1.5, open_interest=10))  # illiquid, closest
    return rows


def test_pick_call_nearest_liquid_inside_target():
    today = date(2026, 3, 4)
    ch = pick_contract(chain(today), direction="long", entry=503.0, target=506.5, today=today)
    assert ch is not None
    assert ch.contract.type == "call" and ch.contract.strike in (502, 504)
    assert ch.breakeven < 506.5


def test_pick_rejects_when_breakeven_outside_target():
    today = date(2026, 3, 4)
    assert pick_contract(chain(today), direction="long", entry=503.0, target=503.0, today=today) is None


def test_pick_put_for_short():
    today = date(2026, 3, 4)
    ch = pick_contract(chain(today), direction="short", entry=503.0, target=498.0, today=today)
    assert ch is not None and ch.contract.type == "put" and ch.breakeven > 498.0


def test_contracts_for_risk():
    assert contracts_for_risk(1000, 2.5) == 4
    assert contracts_for_risk(100, 2.5) == 0
    assert contracts_for_risk(100, 0) == 0


def test_webhook_validate():
    good = {"secret": "s", "symbol": "SPY", "event": "level_break", "direction": "long", "level": 1.0, "ts": "x"}
    assert validate(good, "s") is None
    assert validate({**good, "secret": "wrong"}, "s") == "bad_secret"
    assert validate({**good, "event": "nope"}, "s") == "unknown_event"
    assert validate({k: v for k, v in good.items() if k != "level"}, "s") == "missing_level"
    assert validate(good, "") is None  # no secret configured → not checked


def test_webhook_store_consume_once():
    j = Journal(":memory:")
    st = WebhookStore(j)
    st.put({"symbol": "spy", "event": "level_break", "direction": "long", "level": 1.0}, None)
    st.put({"symbol": "spy", "event": "level_break", "direction": "long", "level": 1.0}, "bad_secret")
    hit = st.consume("SPY", "long")
    assert hit is not None and hit["level"] == 1.0
    assert st.consume("SPY", "long") is None   # consumed; the bad one is never served
