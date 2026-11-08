from datetime import date

from apexpm.calendar import day_context, first_friday, rule_events, third_friday, upcoming
from apexpm.db import Journal


def test_third_friday_and_witching():
    assert third_friday(2026, 3) == date(2026, 3, 20)
    types = [e["type"] for e in rule_events(date(2026, 3, 20))]
    assert "quad_witching" in types
    assert "opex" in [e["type"] for e in rule_events(date(2026, 4, 17))]


def test_fomc_and_windows():
    ctx = day_context(date(2026, 10, 28), ["SPY"])
    assert any(e["type"] == "fomc" for e in ctx.events)
    assert ctx.size_mult == 0.5
    assert ctx.entries_blocked_at("14:00") is not None
    assert ctx.entries_blocked_at("10:00") is None


def test_nfp_first_friday():
    assert first_friday(2026, 11) == date(2026, 11, 6)
    assert "nfp" in [e["type"] for e in rule_events(date(2026, 11, 6))]


def test_earnings_self_skips_symbol():
    j = Journal(":memory:")
    from apexpm.calendar import ensure_table
    ensure_table(j)
    with j.tx() as c:
        c.execute("INSERT INTO calendar_events VALUES ('earnings','NVDA','2026-11-19','amc','test','x')")
        c.execute("INSERT INTO calendar_events VALUES ('earnings','TSLA','2026-11-20','bmo','test','x')")
    # NVDA reports after the close on the 19th → affects the 20th; TSLA before the open on the 20th
    ctx = day_context(date(2026, 11, 20), ["TSLA", "SPY"], j)
    types = {(e["type"], e.get("symbol")) for e in ctx.events}
    assert ("earnings_mover", "NVDA") in types and ("earnings_self", "TSLA") in types
    assert "TSLA" in ctx.skip_symbols and "SPY" not in ctx.skip_symbols
    assert ctx.size_mult == 0.75


def test_upcoming_lists_events():
    ev = upcoming(None, date(2026, 11, 2), 30, ["SPY"])
    assert any(e["type"] == "opex" and e["date"] == "2026-11-20" for e in ev)
    assert any(e["type"] == "half_day" and e["date"] == "2026-11-27" for e in ev)


def test_alerts_window_and_flash():
    from datetime import datetime
    from apexpm.calendar import alerts
    # Thu 2026-11-19 13:00 ET: OPEX is Fri 11-20 → tomorrow, afternoon-before → flash
    now = datetime(2026, 11, 19, 13, 0)
    al = alerts(None, now, ["SPY"])
    opex = [a for a in al if a["type"] == "opex"]
    assert opex and opex[0]["severity"] == "tomorrow" and opex[0]["flash"] is True
    # Wed 11-18 09:00: OPEX is in 2 days → soon, no flash
    al2 = alerts(None, datetime(2026, 11, 18, 9, 0), ["SPY"])
    o2 = [a for a in al2 if a["type"] == "opex"][0]
    assert o2["severity"] == "soon" and o2["flash"] is False
