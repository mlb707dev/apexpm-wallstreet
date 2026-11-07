"""End-of-day review: discipline, regime accuracy, rejections, auto-suspension."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def greeks_line(t) -> str:
    """'greeks at buy Δ0.52 Γ0.08 Θ-0.21 ν0.05 IV 18% VIX 16.2 → at sell Δ0.61 … ' from the journal notes."""
    import json as _json
    try:
        n = _json.loads(t["notes"] or "{}")
    except Exception:  # noqa: BLE001
        return "greeks: n/a"

    def fmt(g, vix):
        if not g:
            return "n/a"
        parts = [f"Δ{g['delta']:+.2f}" if g.get("delta") is not None else "",
                 f"Γ{g['gamma']:.3f}" if g.get("gamma") is not None else "",
                 f"Θ{g['theta']:+.2f}" if g.get("theta") is not None else "",
                 f"ν{g['vega']:.2f}" if g.get("vega") is not None else "",
                 f"IV {g['iv']:.0%}" if g.get("iv") is not None else ""]
        if vix is not None:
            parts.append(f"VIX {vix:.1f}")
        return " ".join(p for p in parts if p)
    return f"greeks at buy {fmt(n.get('greeks_entry'), n.get('vix_entry'))} → at sell {fmt(n.get('greeks_exit'), n.get('vix_exit'))}"


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
        if (t["asset"] or "stock") == "option":
            lines.append("  " + greeks_line(t))
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
    from . import variants as variantsmod
    vrows = variantsmod.rows(j)
    if vrows:
        lines += ["", "## Tuned variants (shadow until promoted)", ""]
        for v in vrows:
            shadow = j.q1("SELECT COUNT(*) AS n FROM rejections WHERE session_date=? AND setup=? AND reason='research_observed'", (date, v["name"]))
            t = (v.get("evidence") or {}).get("pooled", {}).get("test", {})
            lines.append(f"- {v['name']} ({v['base']}): {reg.status(v['name']) if v['name'] in reg._entries else 'retired'}, "
                         f"symbols {', '.join(v['symbols'])}, test {t.get('exp', 0):+.3f}R n{t.get('n', 0)}, shadow signals today {shadow['n'] if shadow else 0}")
    actions = reg.review()
    lines += ["", "## Governance actions", ""] + ([f"- {a}" for a in actions] or ["- none"])
    pend = reg.decisions("pending")
    lines += ["", "## Decisions waiting for the owner", ""] + ([f"- #{d['id']} {d['title']}: {d['proposal']}" for d in pend] or ["- none"])
    from .readiness import readiness, readiness_lines
    lines += [""] + readiness_lines(readiness(j, bot.cfg))
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
