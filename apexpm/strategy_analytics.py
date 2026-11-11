"""Per-strategy analytics for the dashboard: the three tiers (active = live book, paper book,
lab = shadow book), each strategy's attainment trend, biggest win and loss in dollars and percent,
and the trade history behind every number so the owner can sift through it.

Everything here is read-only over the journal. Nothing trades, promotes or demotes.

Definitions
  attainment   share of closed trades that exited at the target (target / runner_target), the bar a
               strategy is judged on: did it collect what it set out to collect
  reached      share of closed trades whose price reached the targeted level at some point (hit or not)
  pct          a trade's P&L as a percent of the capital it tied up (entry x qty, x100 for options)
  trend        last ROLL trades against the ROLL before them (expectancy R and attainment); "up", "down",
               "flat" or "n/a" when there is not enough history
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

ROLL = 10              # rolling window for the trend
TIERS = (("active", "live", "LIVE"), ("paper", "paper", "PAPER"), ("lab", "shadow", "DRY_RUN"))
TARGET_EXITS = ("target", "runner_target")


def _notes(t: dict) -> dict:
    try:
        return json.loads(t.get("notes") or "{}") or {}
    except Exception:  # noqa: BLE001
        return {}


def _cost(t: dict) -> float:
    mult = 100.0 if (t.get("asset") or "stock") == "option" else 1.0
    return abs(float(t["entry"] or 0.0) * float(t["qty"] or 0.0) * mult)


def enrich(t: dict) -> dict:
    """A closed-or-open trade row with the derived fields the analytics and the drill-down share."""
    r = dict(t)
    cost = _cost(r)
    pnl = r.get("pnl")
    r["cost"] = round(cost, 2)
    r["pnl_pct"] = round(100.0 * float(pnl) / cost, 2) if (pnl is not None and cost > 0) else None
    r["attained"] = bool(r.get("exit_reason") in TARGET_EXITS) if r.get("closed_at") else None
    r["reached"] = (bool(r.get("level_reached") is not None and r.get("level_targeted")
                         and float(r["level_reached"]) >= float(r["level_targeted"])) if r.get("closed_at") else None)
    try:
        from datetime import datetime
        a, b = r.get("opened_at"), r.get("closed_at")
        r["held_min"] = int((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60) if a and b else None
    except Exception:  # noqa: BLE001
        r["held_min"] = None
    n = _notes(r)
    r["conviction"] = n.get("conviction")
    r["trigger"] = n.get("trigger")
    r["vix_entry"] = n.get("vix_entry")
    r.pop("notes", None)
    return r


def _trend(now: List[float], before: List[float], tol: float) -> str:
    if len(now) < 3 or len(before) < 3:
        return "n/a"
    a, b = sum(now) / len(now), sum(before) / len(before)
    return "up" if a - b > tol else "down" if b - a > tol else "flat"


def summarize(rows: List[dict]) -> Dict[str, Any]:
    """Summary of one strategy over its closed trades (already enriched, oldest first)."""
    rs = [float(r["rr_realized"]) for r in rows if r.get("rr_realized") is not None]
    pnls = [float(r["pnl"] or 0.0) for r in rows]
    n = len(rows)
    wins = [x for x in rs if x > 0]
    losses = [-x for x in rs if x <= 0]
    att = [1.0 if r["attained"] else 0.0 for r in rows]
    reached = [1.0 if r["reached"] else 0.0 for r in rows]

    def pick(key, best: bool):
        cands = [r for r in rows if r.get(key) is not None]
        if not cands:
            return None
        r = max(cands, key=lambda r: r[key]) if best else min(cands, key=lambda r: r[key])
        return {"id": r["id"], "symbol": r["symbol"], "closed_at": r["closed_at"], "pnl": round(float(r["pnl"] or 0), 2),
                "pnl_pct": r["pnl_pct"], "r": round(float(r["rr_realized"]), 3) if r.get("rr_realized") is not None else None,
                "exit_reason": r["exit_reason"], "mode": r["mode"]}

    cum_r = cum_p = 0.0
    curve = []
    for r in rows:
        cum_r += float(r["rr_realized"] or 0.0)
        cum_p += float(r["pnl"] or 0.0)
        curve.append({"id": r["id"], "t": r["closed_at"], "cum_r": round(cum_r, 3), "cum_pnl": round(cum_p, 2),
                      "attained": r["attained"]})
    # rolling attainment and expectancy, one point per closed trade once ROLL trades exist
    roll = []
    for i in range(ROLL - 1, n):
        w = rows[i - ROLL + 1:i + 1]
        wr = [float(x["rr_realized"] or 0.0) for x in w]
        roll.append({"t": rows[i]["closed_at"], "attain": round(sum(1 for x in w if x["attained"]) / ROLL, 2),
                     "exp_r": round(sum(wr) / ROLL, 3)})
    big_win_usd, big_loss_usd = pick("pnl", True), pick("pnl", False)
    if big_win_usd and big_win_usd["pnl"] <= 0:
        big_win_usd = None
    if big_loss_usd and big_loss_usd["pnl"] >= 0:
        big_loss_usd = None
    big_win_pct, big_loss_pct = pick("pnl_pct", True), pick("pnl_pct", False)
    if big_win_pct and (big_win_pct["pnl_pct"] or 0) <= 0:
        big_win_pct = None
    if big_loss_pct and (big_loss_pct["pnl_pct"] or 0) >= 0:
        big_loss_pct = None
    return {
        "n": n, "wins": len(wins), "win_rate": round(len(wins) / n, 3) if n else None,
        "exp_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_r": round(sum(rs), 2), "pnl": round(sum(pnls), 2),
        "pf": round(sum(wins) / sum(losses), 2) if sum(losses) > 0 else None,      # None with wins > 0 means no losses yet
        "attainment": round(sum(att) / n, 3) if n else None,
        "reached": round(sum(reached) / n, 3) if n else None,
        "gave_back": int(sum(1 for r in rows if r["reached"] and not r["attained"])),
        "avg_win_pct": round(sum(r["pnl_pct"] for r in rows if (r["pnl_pct"] or 0) > 0) / len(wins), 2) if wins and all(r["pnl_pct"] is not None for r in rows) else None,
        "avg_loss_pct": round(sum(r["pnl_pct"] for r in rows if (r["pnl_pct"] or 0) <= 0) / len(losses), 2) if losses and all(r["pnl_pct"] is not None for r in rows) else None,
        "last_roll_exp_r": round(sum(rs[-ROLL:]) / len(rs[-ROLL:]), 3) if rs else None,
        "last_roll_attain": round(sum(att[-ROLL:]) / len(att[-ROLL:]), 2) if att else None,
        "trend_exp": _trend(rs[-ROLL:], rs[-2 * ROLL:-ROLL], 0.05),
        "trend_attain": _trend(att[-ROLL:], att[-2 * ROLL:-ROLL], 0.1),
        "biggest_win": {"usd": big_win_usd, "pct": big_win_pct},
        "biggest_loss": {"usd": big_loss_usd, "pct": big_loss_pct},
        "last_closed_at": rows[-1]["closed_at"] if rows else None,
        "curve": curve, "rolling": roll,
    }


def _closed_for(journal, name: str, since: Optional[str]) -> List[dict]:
    rows = [enrich(dict(r)) for r in journal.closed_trades(name) if r["pnl"] is not None]
    if since:
        rows = [r for r in rows if (r["closed_at"] or "") >= since]
    return rows


def _since(days: Optional[int]) -> Optional[str]:
    if not days:
        return None
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _earned(journal, name: str, registry):
    try:
        from . import earned
        if registry.book(name) not in ("paper", "live"):
            return None
        return earned.ladder(journal, name, 30)
    except Exception:  # noqa: BLE001
        return None


def tiers(journal, registry, days: Optional[int] = None) -> Dict[str, Any]:
    """The three sections. A strategy is judged on the trades of the book it sits in now (a paper
    strategy on its PAPER fills, a lab strategy on its shadow journal), with the counts of its other
    modes shown so a promotion's history is not lost. All time by default; days= narrows the window."""
    since = _since(days)
    from . import variants as variantsmod
    expected: Dict[str, float] = {}
    try:
        for v in variantsmod.rows(journal, active_only=False):
            t = (v.get("evidence") or {}).get("pooled", {}).get("test", {})
            if t.get("exp") is not None:
                expected[v["name"]] = float(t["exp"])
    except Exception:  # noqa: BLE001
        pass
    out: Dict[str, Any] = {"since": since, "days": days or 0, "tiers": []}
    try:
        from . import brain as brainmod
        lessons = {l["name"]: l for l in brainmod.lessons(journal)}
    except Exception:  # noqa: BLE001
        lessons = {}
    open_by: Dict[str, int] = {}
    for r in journal.open_trades():
        open_by[r["setup"]] = open_by.get(r["setup"], 0) + 1
    for tier, book, mode in TIERS:
        items = []
        for name in registry.names():
            if registry.book(name) != book:
                continue
            allrows = _closed_for(journal, name, since)
            rows = [r for r in allrows if r["mode"] == mode]
            by_mode: Dict[str, int] = {}
            for r in allrows:
                by_mode[r["mode"]] = by_mode.get(r["mode"], 0) + 1
            s = summarize(rows)
            e = registry._entries[name]
            items.append({"name": name, "status": registry.status(name), "label": registry.label(name), "book": book,
                          "description": e.strategy.description, "symbols": list(e.symbols), "mode": mode,
                          "tags": list(getattr(e.strategy, "tags", ()) or ()),
                          "open": open_by.get(name, 0), "n_by_mode": by_mode, "expected_r": expected.get(name),
                          "drift_r": (round(s["exp_r"] - expected[name], 3) if (name in expected and s["exp_r"] is not None) else None),
                          "lessons": ({"summary": lessons[name].get("summary"), "recommendations": lessons[name].get("recommendations", []),
                                       "verdicts": lessons[name].get("verdicts"), "n_with_bars": lessons[name].get("n_with_bars"),
                                       "patterns": lessons[name].get("patterns")}
                                      if name in lessons else None),
                          "earned": _earned(journal, name, registry),
                          **s})
        items.sort(key=lambda x: (-(x["pnl"] or 0.0), -(x["n"] or 0), x["name"]))
        out["tiers"].append({"tier": tier, "book": book, "mode": mode, "strategies": items,
                             "n": sum(i["n"] for i in items), "pnl": round(sum(i["pnl"] for i in items), 2)})
    return out


def trades_for(journal, name: str, mode: str = "", limit: int = 500, days: Optional[int] = None) -> Dict[str, Any]:
    """Every trade of one strategy, newest first, with the derived fields and its replay, for the drill-down."""
    since = _since(days)
    rows = [enrich(dict(r)) for r in journal.q("SELECT * FROM trades WHERE setup=? ORDER BY id DESC LIMIT ?", (name, int(limit)))]
    try:
        from . import postmortem as pm
        revs = {r["trade_id"]: r["review"] for r in pm.reviews(journal, name)}
        for r in rows:
            rv = revs.get(r["id"])
            r["review"] = rv
            r["review_text"] = pm.verdict_text(rv) if rv else None
    except Exception:  # noqa: BLE001
        pass
    if mode:
        rows = [r for r in rows if r["mode"] == mode]
    if since:
        rows = [r for r in rows if r["status"] != "closed" or (r["closed_at"] or "") >= since]
    closed = sorted([r for r in rows if r["status"] == "closed" and r["pnl"] is not None], key=lambda r: r["closed_at"] or "")
    s = summarize(closed)
    symbols = sorted({r["symbol"] for r in rows})
    reasons = sorted({r["exit_reason"] for r in rows if r.get("exit_reason")})
    modes = sorted({r["mode"] for r in rows})
    return {"name": name, "since": since, "summary": s, "trades": rows, "symbols": symbols, "exit_reasons": reasons, "modes": modes}


# ---- the master view: every closed trade, every book, with the traits the brain slices by ----
def _asset_class(t: dict) -> str:
    sym = t.get("symbol") or ""
    if "/" in sym:
        return "crypto"
    return "options" if (t.get("asset") or "stock") == "option" else "stocks"


def _book_of(mode: str) -> str:
    return {"LIVE": "real money", "PAPER": "practice", "DRY_RUN": "lab"}.get(mode or "", "lab")


def master_trades(journal, days: Optional[int] = None, limit: int = 20000) -> Dict[str, Any]:
    """Every closed trade with its traits, for the single master view. Grouping happens in the page so
    switching the lens is instant. Source is tagged so other bots' journals can join the same view later."""
    since = _since(days)
    rows = [dict(r) for r in journal.q("SELECT * FROM trades WHERE status='closed' AND pnl IS NOT NULL ORDER BY closed_at DESC LIMIT ?", (int(limit),))]
    if since:
        rows = [r for r in rows if (r.get("closed_at") or "") >= since]
    try:
        from . import postmortem as pm
        verdicts = {r["trade_id"]: (r["review"] or {}).get("verdict") for r in pm.reviews(journal)}
    except Exception:  # noqa: BLE001
        verdicts = {}
    out = []
    for raw in rows:
        n = _notes(raw)
        t = enrich(raw)
        mult = 100.0 if (raw.get("asset") or "stock") == "option" else 1.0
        stop0 = raw.get("initial_stop") or raw.get("stop") or 0.0
        risk_usd = abs(float(raw["entry"] or 0) - float(stop0 or 0)) * float(raw["qty"] or 0) * mult
        out.append({
            "id": t["id"], "source": "wallstreet", "strategy": t["setup"], "book": _book_of(t.get("mode")),
            "asset": _asset_class(raw), "symbol": t["symbol"], "side": "buy" if t["direction"] == "long" else "short",
            "opened_at": t["opened_at"], "closed_at": t["closed_at"], "held_min": t.get("held_min"),
            "r": round(float(t["rr_realized"]), 3) if t.get("rr_realized") is not None else None,
            "pnl": round(float(t["pnl"]), 2), "pnl_pct": t.get("pnl_pct"), "risk_usd": round(risk_usd, 2),
            "capital_usd": t.get("cost"), "ended": t.get("exit_reason"), "hit_target": t.get("attained"),
            "regime": t.get("regime"), "confirmations": n.get("conviction"), "session": n.get("session"),
            "size_mult": n.get("earned_mult"), "verdict": verdicts.get(t["id"]),
        })
    return {"since": since, "days": days or 0, "n": len(out), "trades": out,
            "sources": sorted({x["source"] for x in out}) or ["wallstreet"]}


def stats_trades(journal, limit: int = 20000) -> Dict[str, Any]:
    """Every trade (closed and still open) in every book for the Stats tab: plain facts per trade so the
    page can window (24h / 5 trading days / 30 days / all time), split live vs shadow and slice instantly.
    "Live book" = orders actually placed (paper now, real money later); "shadow book" = the lab."""
    rows = [dict(r) for r in journal.q(
        "SELECT * FROM trades WHERE (status='closed' AND pnl IS NOT NULL) OR status='open' ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
        (int(limit),))]
    out = []
    for raw in rows:
        t = enrich(raw)
        book = _book_of(t.get("mode"))
        out.append({
            "id": t["id"], "status": "open" if t.get("status") == "open" else "closed",
            "book": "shadow" if book == "lab" else "live", "account": book,
            "strategy": t["setup"], "symbol": t["symbol"], "asset": _asset_class(raw),
            "side": "buy" if t["direction"] == "long" else "short", "qty": t.get("qty"),
            "entry": t.get("entry"), "exit": t.get("exit_price"), "stop": t.get("stop"), "target": t.get("target"),
            "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"), "held_min": t.get("held_min"),
            "pnl": round(float(t["pnl"]), 2) if t.get("pnl") is not None else None, "pnl_pct": t.get("pnl_pct"),
            "r": round(float(t["rr_realized"]), 3) if t.get("rr_realized") is not None else None,
            "ended": t.get("exit_reason"), "cost": t.get("cost"),
        })
    return {"n": len(out), "trades": out}
