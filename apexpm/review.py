"""End-of-day review: discipline, regime accuracy, rejections, auto-suspension."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def daily_review(bot, date_iso: str = None) -> str:
    j = bot.journal
    reg = bot.registry
    date_iso = date_iso or datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    rows = [t for t in j.closed_trades() if t["session_date"] == date_iso]
    opened = j.trades_today(date_iso)
    pnl = j.realized_pnl_today(date_iso)
    wins = sum(1 for t in rows if (t["pnl"] or 0) > 0)
    overholds = sum(1 for t in rows if t["level_reached"] is not None and t["level_targeted"]
                    and t["level_reached"] > t["level_targeted"] + 0.1 and t["exit_reason"] != "target")
    stopped_then_target = 0  # placeholder until scans carry post-exit path
    rej = j.rejection_histogram(date_iso)

    # regime accuracy: did the day's dominant direction match the plan's htf regime?
    scans = j.q("SELECT regime, price FROM scans WHERE session_date=? ORDER BY id", (date_iso,))
    regime_note = "n/a"
    if len(scans) >= 2:
        first, last = scans[0], scans[-1]
        move = (last["price"] or 0) - (first["price"] or 0)
        called = first["regime"]
        actual = "trend_up" if move > 0 else "trend_down"
        regime_note = f"called {called}, day moved {'up' if move > 0 else 'down'} ({'match' if called == actual else 'miss' if called != 'chop' else 'chop call'})"

    start_eq = j.get_state("starting_equity")
    end_eq = bot.state.equity if bot.state else None
    lines: List[str] = [f"# Daily review {date_iso}", "",
                        f"- opened {opened}, closed {len(rows)}, wins {wins}, realized P&L {pnl:+.2f}",
                        f"- overholds past target: {overholds}",
                        f"- regime: {regime_note}",
                        f"- equity: start {start_eq}, now {end_eq}", ""]
    lines += ["## Closed trades", ""]
    for t in rows:
        lines.append(f"- #{t['id']} {t['setup']} {t['direction']} {t['symbol']} entry {t['entry']} exit {t['exit_price']} "
                     f"({t['exit_reason']}) rr {t['rr_realized']:+.2f} level {t['level_reached']}/{t['level_targeted']}")
    if not rows:
        lines.append("- none")
    lines += ["", "## Rejections", ""]
    for k, v in sorted(rej.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}")
    if not rej:
        lines.append("- none")
    lines += ["", "## Strategies", ""]
    for n in reg.names():
        st = reg.stats(n)
        base = f"- {n}: {reg.status(n)}"
        if st:
            base += (f" | last {st.trades}: win {st.win_rate:.0%}, exp {st.expectancy_r:+.2f}R, "
                     f"pf {st.profit_factor:.2f}, dd {st.max_dd_r:.1f}R, discipline {st.level_discipline:.0%}")
        lines.append(base)
    actions = reg.review()
    lines += ["", "## Governance actions", ""] + ([f"- {a}" for a in actions] or ["- none"])
    pend = reg.decisions("pending")
    lines += ["", "## Decisions waiting for the owner", ""] + ([f"- #{d['id']} {d['title']}: {d['proposal']}" for d in pend] or ["- none"])
    report = "\n".join(lines)
    try:
        with j.tx() as c:
            c.execute("INSERT OR REPLACE INTO daily_reports (date, starting_equity, ending_equity, realized_pnl, "
                      "trades, wins, overholds, rejections, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                      (date_iso, float(start_eq) if start_eq else None, end_eq, pnl, len(rows), wins,
                       overholds, sum(rej.values()), report))
    except Exception as exc:  # noqa: BLE001
        bot.log.warning("daily report save failed: %s", exc)
    return report
