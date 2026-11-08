"""Auditor: the harness role that checks the book is earning what the evidence promised and is
not giving profits back. Runs on the bot (it needs the journal), in the daily review and on
/api/audit. It never trades and never demotes on its own: a strategy that is losing with enough
trades to mean it becomes a pending decision for the owner (registry.propose), journaled.

Per strategy in the paper/live books (all closed trades, or the last N days when asked):
  n, expectancy R, profit factor, win rate, last-10 expectancy
  overhold share    trades that reached the target level but did not exit at target (gave the move back)
  stop share        trades that ended on the initial stop
  expected R        the time-split test expectancy the variant was promoted on, when one is stored
  drift             realized minus expected (needs n >= DRIFT_MIN_N to mean anything)
Per session day: realized P&L path from closed trades -> peak gain vs close of day; a day that
closed below half of a positive peak "gave back".
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

DEMOTE_MIN_N = 20          # trades since the ladder reset before a losing strategy is put to the owner
DEMOTE_MAX_EXP = -0.10     # R
DEMOTE_MAX_PF = 0.85
DRIFT_MIN_N = 15


def _trade_rows(journal, name: str, since: str) -> List[dict]:
    rows = [dict(r) for r in journal.closed_trades(name) if r["mode"] in ("PAPER", "LIVE") and r["rr_realized"] is not None]
    if since:
        rows = [r for r in rows if (r["closed_at"] or "") >= since]
    return rows


def strategy_audit(journal, registry, name: str, since: str, expected_r=None) -> Dict[str, Any]:
    rows = _trade_rows(journal, name, since)
    n = len(rows)
    rs = [float(r["rr_realized"]) for r in rows]
    exp = sum(rs) / n if n else None
    gains = sum(x for x in rs if x > 0)
    losses = -sum(x for x in rs if x < 0)
    pf = (gains / losses) if losses > 0 else (float("inf") if gains > 0 else None)
    win = (sum(1 for x in rs if x > 0) / n) if n else None
    last10 = rs[-10:]
    overhold = sum(1 for r in rows if r.get("level_reached") is not None and r.get("level_targeted")
                   and r["level_reached"] >= r["level_targeted"] and r["exit_reason"] not in ("target", "runner_target"))
    stopped = sum(1 for r in rows if r["exit_reason"] == "stop")
    out = {"name": name, "status": registry.status(name), "n": n,
           "exp_r": round(exp, 3) if exp is not None else None,
           "pf": (round(pf, 2) if pf not in (None, float("inf")) else pf), "win": round(win, 2) if win is not None else None,
           "last10_exp_r": round(sum(last10) / len(last10), 3) if last10 else None,
           "overhold_share": round(overhold / n, 2) if n else None, "stop_share": round(stopped / n, 2) if n else None,
           "expected_r": expected_r, "drift_r": None, "flags": []}
    if expected_r is not None and exp is not None and n >= DRIFT_MIN_N:
        out["drift_r"] = round(exp - float(expected_r), 3)
        if out["drift_r"] < -0.15:
            out["flags"].append("below_evidence")
    if n and out["overhold_share"] is not None and out["overhold_share"] >= 0.3:
        out["flags"].append("gives_back_targets")
    if n >= DEMOTE_MIN_N and exp is not None and exp <= DEMOTE_MAX_EXP and (pf is not None and pf != float("inf") and pf <= DEMOTE_MAX_PF):
        out["flags"].append("no_edge")
    return out


def day_giveback(journal, since: str) -> Dict[str, Any]:
    """Realized P&L path per session date: peak cumulative gain vs end of day."""
    rows = [dict(r) for r in journal.closed_trades() if r["mode"] in ("PAPER", "LIVE") and r["pnl"] is not None]
    if since:
        rows = [r for r in rows if (r["closed_at"] or "") >= since]
    by_day: Dict[str, List[dict]] = {}
    for r in sorted(rows, key=lambda r: r["closed_at"] or ""):
        by_day.setdefault(r["session_date"], []).append(r)
    days = []
    for d, trs in sorted(by_day.items()):
        cum = peak = 0.0
        for t in trs:
            cum += float(t["pnl"] or 0.0)
            peak = max(peak, cum)
        gave_back = peak > 0 and cum < 0.5 * peak
        days.append({"date": d, "peak": round(peak, 2), "close": round(cum, 2), "gave_back": gave_back})
    return {"days": days, "gave_back_days": sum(1 for x in days if x["gave_back"]), "total_days": len(days)}


def _since_days(days: Optional[int]) -> str:
    if not days:
        return ""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def audit(journal, registry, cfg, days: Optional[int] = None) -> Dict[str, Any]:
    """All-time by default (the owner's rule: learn from every trade); days= narrows the window."""
    since = _since_days(days)
    from . import variants as variantsmod
    expected = {}
    for v in variantsmod.rows(journal):
        t = (v.get("evidence") or {}).get("pooled", {}).get("test", {})
        if t.get("exp") is not None:
            expected[v["name"]] = float(t["exp"])
    strategies = []
    proposals = []
    for name in registry.names():
        if registry.book(name) not in ("paper", "live"):
            continue
        a = strategy_audit(journal, registry, name, since, expected.get(name))
        strategies.append(a)
        if "no_edge" in a["flags"]:
            title = f"demote {name} to shadow"
            detail = (f"{a['n']} trades since the ladder reset: expectancy {a['exp_r']:+.2f}R, PF {a['pf']}, "
                      f"last 10 {a['last10_exp_r']:+.2f}R. Owner rule: only no-edge strategies move to shadow, and only by decision.")
            did = registry.propose("auditor", name, title, detail, "set status research (shadow) until a tuned variant earns its way back")
            proposals.append({"id": did, "title": title})
    gb = day_giveback(journal, since)
    out = {"since": since or None, "strategies": strategies, "giveback": gb, "proposals": proposals}
    try:
        with journal.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'auditor', 'audit', ?)",
                      (json.dumps({"strategies": [(s["name"], s["n"], s["exp_r"], s["flags"]) for s in strategies],
                                   "gave_back_days": gb["gave_back_days"], "proposals": len(proposals)}),))
    except Exception:  # noqa: BLE001
        pass
    return out


def audit_lines(a: Dict[str, Any]) -> List[str]:
    lines = ["## Auditor (book vs evidence, all time)", ""]
    if not a["strategies"]:
        lines.append("- no strategy in the paper/live books")
    for s in a["strategies"]:
        if not s["n"]:
            lines.append(f"- {s['name']} ({s['status']}): no closed trades yet")
            continue
        exp_txt = f", evidence {s['expected_r']:+.3f}R drift {s['drift_r']:+.3f}R" if s["drift_r"] is not None else \
                  (f", evidence {s['expected_r']:+.3f}R (n < {DRIFT_MIN_N})" if s["expected_r"] is not None else "")
        lines.append(f"- {s['name']} ({s['status']}): n {s['n']}, exp {s['exp_r']:+.3f}R, pf {s['pf']}, win {s['win']:.0%}, "
                     f"last10 {s['last10_exp_r']:+.3f}R, overhold {s['overhold_share']:.0%}, stopped {s['stop_share']:.0%}{exp_txt}"
                     + (f" | FLAGS: {', '.join(s['flags'])}" if s["flags"] else ""))
    gb = a["giveback"]
    lines.append(f"- days that gave back more than half a positive peak: {gb['gave_back_days']} of {gb['total_days']}")
    for p in a["proposals"]:
        lines.append(f"- proposed decision #{p['id']}: {p['title']}")
    return lines
