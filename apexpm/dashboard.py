"""Dashboard API + static app (PWA).

Auth: when DASHBOARD_TOKEN is set, every /api call needs it as
`Authorization: Bearer <token>`, or the `apexpm_token` cookie, or `?token=`
once (which sets the cookie). Without a token the server only binds to
localhost (enforced by config.validate and serve()).

CORS is open for the API so a static front-end on another origin
can call it with the bearer token. Secrets are never read or returned here.
"""
from __future__ import annotations

import hmac
import json
import math
import logging
import mimetypes
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import calendar as cal
from . import profiles
from .news import NewsFeed
from .config import Config
from .db import Journal, et_date
from .registry import Registry
from .strategies import ExampleBreakout
from . import variants as variantsmod

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
# The full app lives outside the statically hosted folder and is served only behind the token: the
# public host gets a login shell that fetches /app with the token (owner, 2026-11-07: nothing about the
# build or the strategies is readable without the token).
APP_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webapp", "app.html")
STATIC_FILES = {"/manifest.json": "manifest.json", "/sw.js": "sw.js"}


def _rows(rows) -> list:
    return [dict(r) for r in rows]


class Api:
    def __init__(self, cfg: Config, journal: Journal):
        self.cfg = cfg
        self.j = journal
        self.reg = Registry(journal, cfg)
        self.reg.register(ExampleBreakout(), "paper")
        variantsmod.register_all(self.reg, self.j)
        cal.ensure_table(journal)
        self._news = NewsFeed(cfg, journal) if cfg.news_enabled else None
        self._broker = None
        if cfg.alpaca_api_key and cfg.alpaca_secret_key:
            try:
                from .broker import Broker
                self._broker = Broker(cfg)
            except Exception:  # noqa: BLE001
                self._broker = None

    # ---- helpers ------------------------------------------------------------
    def _last_price(self, symbol: str) -> Optional[float]:
        v = self.j.get_state(f"last_price:{symbol}")
        return float(v) if v else None

    def _account(self) -> Dict[str, Any]:
        if not self._broker:
            return {"connected": False}
        try:
            a = self._broker.account()
            return {"connected": True, "equity": a.equity, "cash": a.cash, "buying_power": a.buying_power,
                    "paper": self.cfg.paper}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)[:120]}

    def _curve(self, closed: List[dict]) -> List[dict]:
        start = float(self.j.get_state("starting_equity") or 0)
        cum, cum_pnl, out = 0.0, 0.0, []
        for t in closed:
            cum += t["rr_realized"] or 0
            cum_pnl += t["pnl"] or 0
            out.append({"t": t["closed_at"], "r": round(cum, 3), "pnl": t["pnl"], "setup": t["setup"],
                        "equity": round(start + cum_pnl, 2) if start else None, "cum_pnl": round(cum_pnl, 2)})
        return out

    # ---- endpoints ----------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        closed = _rows(self.j.q("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at"))
        open_ = _rows(self.j.q("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at"))
        rs = [t["rr_realized"] for t in closed if t["rr_realized"] is not None]
        pnl = sum(t["pnl"] or 0 for t in closed)
        wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        cum, peak, dd = 0.0, 0.0, 0.0
        for r in rs:
            cum += r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        today = et_date()
        day_pnl = self.j.realized_pnl_today(today)
        trades_today = self.j.trades_today(today)
        start = self.j.get_state("starting_equity")
        acct0 = self._account()
        band = profiles.EffectiveConfig(self.cfg, profiles.get_profile(self.j, acct0.get("equity")), 1.0, acct0.get("equity") if acct0.get("connected") else None)
        cap = 0.0
        if band.max_daily_loss_pct > 0 and start:
            cap = min(self.cfg.max_daily_loss, float(start) * band.max_daily_loss_pct)
        in_open = 0.0
        unreal = 0.0
        for t in open_:
            if t["status"] != "open":
                continue
            px = self._last_price(t["symbol"])
            in_open += t["qty"] * t["entry"]
            if px:
                unreal += (px - t["entry"]) * (1 if t["direction"] == "long" else -1) * t["qty"]
        start = self.j.get_state("starting_equity")
        return {
            "mode": self.cfg.mode, "paper": self.cfg.paper, "dry_run": self.cfg.dry_run, "symbols": self.cfg.symbols,
            "account": self._account(), "starting_equity": float(start) if start else None,
            "ladder_start": float(self.j.get_state("ladder_start") or start or 0) or None,
            "today": {"date": today, "realized_pnl": round(day_pnl, 2), "trades": trades_today,
                      "loss_budget_left": round(max(0.0, cap + day_pnl), 2) if cap > 0 else None,   # None = no daily stop (hyper band)
                      "max_daily_loss": round(cap, 2) if cap > 0 else None,
                      "open_positions": sum(1 for t in open_ if t["status"] == "open"),
                      "pending": sum(1 for t in open_ if t["status"] == "pending"),
                      "max_positions": band.max_positions, "in_open_notional": round(in_open, 2),
                      "unrealized_pnl": round(unreal, 2)},
            "all_time": {"closed": len(closed), "wins": wins, "win_rate": (wins / len(closed)) if closed else None,
                         "expectancy_r": (sum(rs) / len(rs)) if rs else None, "total_r": round(sum(rs), 3) if rs else 0.0,
                         "max_dd_r": round(dd, 3), "realized_pnl": round(pnl, 2)},
            "curve": self._curve(closed),
            "bot": self.bot(),
        }

    def bot(self) -> Dict[str, Any]:
        hb = self.j.get_state("heartbeat")
        age = None
        if hb:
            try:
                age = int((datetime.now(timezone.utc) - datetime.fromisoformat(hb)).total_seconds())
            except ValueError:
                age = None
        acct = self._account()
        prof = profiles.get_profile(self.j, acct.get("equity") if acct.get("connected") else None)
        eff = profiles.EffectiveConfig(self.cfg, prof, 1.0, acct.get("equity") if acct.get("connected") else None,
                                       positions_exception_on=profiles.positions_exception(self.j))
        return {
            "heartbeat": hb, "heartbeat_age_sec": age, "alive": age is not None and age < 180,
            "market": self.j.get_state("market", "unknown"),
            "regime": {s: self.j.get_state(f"regime:{s}") for s in self.cfg.symbols},
            "last_price": {s: self._last_price(s) for s in self.cfg.symbols},
            "strategies_on": [s.name for s in self.reg.active()],
            "books": {b: [n for n in self.reg.names() if self.reg.book(n) == b] for b in ("live", "paper", "shadow")},
            "pending_decisions": len(self.reg.decisions("pending")),
            "live_armed": [n for n in self.reg.names() if self.reg.mode_for(n) == "LIVE"],
            "profile": prof,
            "effective": {"max_pos_pct": eff.max_pos_pct, "max_trades_per_day": eff.max_trades_per_day,
                          "kelly_fraction": eff.kelly_fraction, "max_positions": eff.max_positions,
                          "max_daily_loss": self.cfg.max_daily_loss, "max_daily_loss_pct": eff.max_daily_loss_pct,
                          "kill_dd_pct": eff.kill_dd_pct, "banger_trigger_pct": eff.banger_trigger_pct,
                          "stop_cooldown_min": self.cfg.stop_cooldown_min},
            "halted": self.j.get_state(f"halted:{et_date()}") == "1",
            "entries_paused": profiles.entries_paused(self.j),
            "positions_exception": profiles.positions_exception(self.j),
            "banger": {"active": self.j.get_state(f"banger_active:{et_date()}") == "1",
                       "peak": float(self.j.get_state(f"banger_peak:{et_date()}", "0") or 0),
                       "trigger_pct": eff.banger_trigger_pct, "floor_pct": self.cfg.banger_floor_pct},   # the band's trigger, not the config default
            "readiness": self.readiness(),
            "max_positions_hard": profiles.MAX_POSITIONS_HARD,
            "key_fingerprint": (self.cfg.alpaca_api_key[:4] + "…") if self.cfg.alpaca_api_key else None,
            "stream": {"ticks": int(self.j.get_state("stream_ticks") or 0), "last_tick": self.j.get_state("stream_last_tick"),
                       "feed": self.cfg.data_feed, "enabled": self.cfg.stream_enabled},
            "day": self._day(),
            "alerts": self._alerts(),
            "volume": self.volume(),
            "vix": json.loads(self.j.get_state("vix") or "null"),
        }

    def _alerts(self) -> list:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
        try:
            return cal.alerts(self.j, now_et, self.cfg.symbols)
        except Exception:  # noqa: BLE001
            return []

    def _day(self) -> Dict[str, Any]:
        raw = self.j.get_state("day_context")
        if raw:
            d = json.loads(raw)
            if d.get("date") == et_date():
                return d
        from datetime import date as _date
        return cal.day_context(_date.fromisoformat(et_date()), self.cfg.symbols, self.j).as_dict()

    def volume(self) -> Dict[str, Any]:
        out = {}
        for s in self.cfg.symbols:
            try:
                v = json.loads(self.j.get_state(f"vol:{s}") or "{}")
                today = float(self.j.get_state(f"vol_today:{s}") or 0)
                v["today"] = today
                v["vs_avg5"] = (today / v["avg5"]) if v.get("avg5") else None
                v["vs_avg30"] = (today / v["avg30"]) if v.get("avg30") else None
                out[s] = v
            except Exception:  # noqa: BLE001
                out[s] = {}
        return out

    def calendar(self) -> Dict[str, Any]:
        from datetime import date as _date
        today = _date.fromisoformat(et_date())
        return {"today": cal.day_context(today, self.cfg.symbols, self.j).as_dict(),
                "upcoming": cal.upcoming(self.j, today, 30, self.cfg.symbols),
                "policy": {k: {"label": v["label"], "size_mult": v["size_mult"], "no_entry": v["no_entry"]} for k, v in cal.POLICY.items()}}

    def decisions(self) -> Dict[str, Any]:
        return {"pending": [dict(r) for r in self.reg.decisions("pending")],
                "resolved": [dict(r) for r in self.reg.decisions("approved")][:20] + [dict(r) for r in self.reg.decisions("dismissed")][:20]}

    def resolve_decision(self, decision_id: int, status: str, note: str) -> Dict[str, Any]:
        if status not in ("approved", "dismissed"):
            return {"ok": False, "error": "bad_status"}
        self.reg.resolve(int(decision_id), status, f"dashboard: {note}" if note else "dashboard")
        return {"ok": True}

    def news(self) -> list:
        if not self._news:
            return []
        out = []
        for n in self._news.latest(15, 36):
            try:
                n["symbols"] = json.loads(n.get("symbols") or "[]")
            except Exception:  # noqa: BLE001
                n["symbols"] = []
            out.append(n)
        return out

    def set_positions_exception(self, on: bool, note: str) -> Dict[str, Any]:
        profiles.set_positions_exception(self.j, on, f"dashboard: {note}" if note else "dashboard")
        return {"ok": True, "positions_exception": on}

    def ledger(self) -> Dict[str, Any]:
        from . import ledger as ledgermod
        return {"flows": ledgermod.flows(self.j, 100), "net": ledgermod.net(self.j), "synced_at": self.j.get_state("ledger_synced_at"),
                "ladder_start": float(self.j.get_state("ladder_start") or 0) or None}

    def record_cash(self, amount: float, kind: str, note: str) -> Dict[str, Any]:
        """Owner records a deposit or withdrawal by hand (the broker sync catches most of them)."""
        from . import ledger as ledgermod
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "error": "amount must be a number"}
        if kind == "withdrawal" and amt > 0:
            amt = -amt
        if kind == "deposit" and amt < 0:
            amt = -amt
        return ledgermod.record(self.j, amt, kind, note=f"dashboard: {note}" if note else "dashboard", source="dashboard", today=et_date())

    def ladder_reset(self, confirm: str, note: str) -> Dict[str, Any]:
        """Owner says the account was reset at the broker. The loop records the equity difference as a
        cash movement on its next pass (baselines move; trade history stays)."""
        if (confirm or "").strip().lower() != "reset":
            return {"ok": False, "error": "type reset to confirm"}
        self.j.set_state("ladder_reset_requested", "1")
        with self.j.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'portfolio', 'ladder_reset_requested', ?)",
                      (f"dashboard: {note}" if note else "dashboard",))
        return {"ok": True, "pending": True}

    def variants(self) -> List[dict]:
        variantsmod.register_all(self.reg, self.j)
        out = []
        for r in variantsmod.rows(self.j, active_only=False):
            st = self.reg.stats(r["name"]) if r["name"] in self.reg._entries else None
            out.append({**r, "status": self.reg.status(r["name"]) if r["name"] in self.reg._entries else "retired",
                        "stats": st.__dict__ if st else None})
        return out

    def submit_variants(self, specs: List[dict], source: str) -> Dict[str, Any]:
        if not isinstance(specs, list) or not specs:
            return {"ok": False, "error": "no variants"}
        for s in specs:
            ev = (s.get("evidence") or {}).get("pooled") or {}
            if not all(k in ev for k in ("train", "val", "test")):
                return {"ok": False, "error": "evidence missing: train/val/test pooled stats required"}
            if not all(float(ev[k].get("exp", 0)) > 0 for k in ("train", "val", "test")):
                return {"ok": False, "error": f"{s.get('base')}: refused, a split is not positive"}
        res = variantsmod.submit(self.reg, self.j, specs, source=source or "tuner")
        return {"ok": True, **res}

    def set_paused(self, paused: bool, note: str) -> Dict[str, Any]:
        profiles.set_entries_paused(self.j, paused, f"dashboard: {note}" if note else "dashboard")
        return {"ok": True, "entries_paused": paused}

    def positions(self) -> List[dict]:
        out = []
        for t in _rows(self.j.q("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at")):
            px = self._last_price(t["symbol"])
            sign = 1 if t["direction"] == "long" else -1
            t["last_price"] = px
            t["unrealized_pnl"] = round((px - t["entry"]) * sign * t["qty"], 2) if px else None
            t["unrealized_r"] = round((px - t["entry"]) * sign / abs(t["entry"] - t["stop"]), 2) if px and t["entry"] != t["stop"] else None
            t["to_target"] = round((t["target"] - px) * sign, 2) if px else None
            t["to_stop"] = round((px - t["stop"]) * sign, 2) if px else None
            try:
                opened = datetime.fromisoformat(t["opened_at"])
                t["held_min"] = int((datetime.now(timezone.utc) - opened).total_seconds() / 60)
            except ValueError:
                t["held_min"] = None
            out.append(t)
        return out

    def trades(self, limit: int = 200) -> list:
        rows = _rows(self.j.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)))
        for r in rows:   # surface greeks/VIX at purchase and sale next to the row
            try:
                n = json.loads(r.get("notes") or "{}")
            except Exception:  # noqa: BLE001
                n = {}
            for k in ("greeks_entry", "greeks_exit", "vix_entry", "vix_exit"):
                r[k] = n.get(k)
        return rows

    def strategies(self) -> list:
        out = []
        variantsmod.register_all(self.reg, self.j)
        vrows = {r["name"]: r for r in variantsmod.rows(self.j, active_only=False)}
        for n in self.reg.names():
            st = self.reg.stats(n)
            e = self.reg._entries[n]
            v = vrows.get(n)
            out.append({"name": n, "description": e.strategy.description, "status": self.reg.status(n),
                        "book": self.reg.book(n), "label": self.reg.label(n),
                        "mode": self.reg.mode_for(n), "symbols": e.symbols, "stats": st.__dict__ if st else None,
                        "earned": self._earned(n),
                        "live_readiness": self.reg.live_readiness(n),
                        "variant": {"base": v["base"], "overrides": v["overrides"], "evidence": v["evidence"], "created_at": v["created_at"]} if v else None})
        return out

    def analytics(self) -> Dict[str, Any]:
        closed = _rows(self.j.q("SELECT * FROM trades WHERE status='closed' AND rr_realized IS NOT NULL ORDER BY closed_at"))

        def agg(key_fn):
            g: Dict[str, List[float]] = {}
            for t in closed:
                g.setdefault(str(key_fn(t)), []).append(t["rr_realized"])
            rows = []
            for k, rs in sorted(g.items()):
                w = [r for r in rs if r > 0]
                l = [-r for r in rs if r <= 0]
                rows.append({"key": k, "n": len(rs), "win": round(len(w) / len(rs), 3), "exp_r": round(sum(rs) / len(rs), 3),
                             "total_r": round(sum(rs), 2), "pf": round(sum(w) / sum(l), 2) if sum(l) else None})
            return rows

        def hour(t):
            try:
                from zoneinfo import ZoneInfo
                return datetime.fromisoformat(t["opened_at"]).astimezone(ZoneInfo("America/New_York")).hour
            except Exception:  # noqa: BLE001
                return "?"

        def dow(t):
            try:
                return datetime.fromisoformat(t["opened_at"]).strftime("%a")
            except Exception:  # noqa: BLE001
                return "?"

        def conv(t):
            try:
                return json.loads(t["notes"] or "{}").get("conviction", "?")
            except Exception:  # noqa: BLE001
                return "?"
        def note(t, key, default="?"):
            try:
                return json.loads(t["notes"] or "{}").get(key, default)
            except Exception:  # noqa: BLE001
                return default

        def delta_bucket(t):
            g = note(t, "greeks_entry", None)
            if not g or g.get("delta") is None:
                return "n/a" if (t["asset"] or "stock") == "option" else "stock"
            d = abs(g["delta"])
            return "|Δ|<0.35" if d < 0.35 else "|Δ| 0.35–0.55" if d < 0.55 else "|Δ|≥0.55"

        def iv_bucket(t):
            g = note(t, "greeks_entry", None)
            if not g or g.get("iv") is None:
                return "n/a" if (t["asset"] or "stock") == "option" else "stock"
            return "IV<15%" if g["iv"] < 0.15 else "IV 15–25%" if g["iv"] < 0.25 else "IV≥25%"

        def vix_bucket(t):
            v = note(t, "vix_entry", None)
            if v is None:
                return "?"
            return "VIX<15" if v < 15 else "VIX 15–20" if v < 20 else "VIX 20–30" if v < 30 else "VIX≥30"

        def asset_class(t):
            a = t["asset"] or "stock"
            return "crypto" if "/" in (t["symbol"] or "") else a

        return {"by_strategy": agg(lambda t: t["setup"]), "by_direction": agg(lambda t: t["direction"]),
                "by_conviction": agg(conv),
                "by_asset_class": agg(asset_class), "by_symbol": agg(lambda t: t["symbol"]),
                "by_strategy_symbol": agg(lambda t: f"{t['setup']} · {t['symbol']}"),
                "by_entry_delta": agg(delta_bucket), "by_entry_iv": agg(iv_bucket), "by_entry_vix": agg(vix_bucket),
                "by_hour": agg(hour), "by_weekday": agg(dow), "by_exit": agg(lambda t: t["exit_reason"]),
                "by_regime": agg(lambda t: t["regime"]), "n": len(closed)}

    def rejection_rows(self, date_iso: str) -> list:
        """Every rejection row for one session date (ts, symbol, setup, direction, reason, detail)."""
        return _rows(self.j.q("SELECT ts, symbol, setup, direction, reason, detail FROM rejections WHERE session_date=? ORDER BY id",
                              (date_iso,)))

    def rejections(self, days: int = 7) -> list:
        return _rows(self.j.q(
            "SELECT session_date AS date, setup, reason, COUNT(*) AS n FROM rejections "
            "WHERE ts >= datetime('now', ?) GROUP BY date, setup, reason ORDER BY date DESC, n DESC", (f"-{days} days",)))

    def events(self) -> list:
        return [dict(r) for r in self.reg.events(100)]

    def readiness(self) -> Dict[str, Any]:
        from .readiness import readiness
        return readiness(self.j, self.cfg)

    def _earned(self, name: str):
        try:
            from . import earned
            return earned.ladder(self.j, name, self.cfg.review_window_trades)
        except Exception:  # noqa: BLE001
            return None

    def strategy_analytics(self, days: int = 0) -> Dict[str, Any]:
        """Three tiers (active = live book, paper, lab = shadow) with attainment trend and extremes per strategy.
        All time by default; days narrows to the last 1/7/30/90 days."""
        from . import strategy_analytics as sa
        variantsmod.register_all(self.reg, self.j)
        return sa.tiers(self.j, self.reg, days=days or None)

    def strategy_trades(self, name: str, mode: str = "", limit: int = 500, days: int = 0) -> Dict[str, Any]:
        """The drill-down: one strategy's trade history with derived fields, replays and filters."""
        from . import strategy_analytics as sa
        return sa.trades_for(self.j, name, mode=mode, limit=limit, days=days or None)

    def brain(self) -> Dict[str, Any]:
        from . import brain as brainmod
        return {"lessons": brainmod.lessons(self.j), "hints": brainmod.hints(self.j)}

    def warden(self) -> Dict[str, Any]:
        from . import warden as wardenmod
        return {"last": wardenmod.last(self.j), "patrol_every_min": wardenmod.PATROL_EVERY_MIN}

    def backup_bytes(self) -> bytes:
        """The whole journal as one SQLite file (consistent snapshot). Token-gated like every /api route."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "apexpm-backup.db")
            self.j.snapshot(p)
            with open(p, "rb") as f:
                data = f.read()
        with self.j.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'backup', 'served', ?)",
                      (json.dumps({"bytes": len(data)}),))
        return data

    def data_status(self) -> Dict[str, Any]:
        """What we hold, is it healthy, when was it last backed up and where."""
        h = self.j.health()
        last_pull = self.j.q1("SELECT ts, detail FROM strategy_events WHERE name='backup' AND event='ran' ORDER BY id DESC LIMIT 1")
        last_served = self.j.q1("SELECT ts FROM strategy_events WHERE name='backup' AND event='served' ORDER BY id DESC LIMIT 1")
        try:
            detail = json.loads(last_pull["detail"]) if last_pull else {}
        except Exception:  # noqa: BLE001
            detail = {}
        return {"journal": h, "last_backup_pulled_at": last_pull["ts"] if last_pull else None, "last_backup_detail": detail,
                "last_backup_served_at": last_served["ts"] if last_served else None}

    def harness_report(self, source: str, detail: dict) -> Dict[str, Any]:
        """A harness job elsewhere (the nightly tuner) reports that it ran, so the warden can see it."""
        src = "".join(ch for ch in str(source) if ch.isalnum() or ch in "_-")[:32] or "harness"
        with self.j.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), ?, 'ran', ?)",
                      (src, json.dumps(detail)[:4000]))
        return {"ok": True, "source": src}

    def audit(self) -> Dict[str, Any]:
        """Auditor pass: book vs evidence, giveback, and any demotion proposals it files (owner decides)."""
        from .auditor import audit
        return audit(self.j, self.reg, self.cfg)

    def reports(self) -> list:
        return _rows(self.j.q("SELECT * FROM daily_reports ORDER BY date DESC LIMIT 30"))

    def weekly_reports(self) -> list:
        try:
            return _rows(self.j.q("SELECT * FROM weekly_reports ORDER BY week_end DESC LIMIT 12"))
        except Exception:  # noqa: BLE001
            return []

    def mark_weekly_emailed(self, week_end: str, by: str) -> Dict[str, Any]:
        """A sender elsewhere reports that it emailed this week's letter."""
        if not week_end:
            return {"ok": False, "error": "week_end required"}
        with self.j.tx() as c:
            cur = c.execute("UPDATE weekly_reports SET emailed_at=? WHERE week_end=? AND emailed_at IS NULL",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), week_end))
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (datetime('now'), 'letter', 'emailed', ?)",
                      (json.dumps({"week_end": week_end, "by": by[:40]}),))
        return {"ok": True, "marked": cur.rowcount == 1}

    def run_weekly(self, week_end: str, email: bool) -> Dict[str, Any]:
        """Owner-triggered recap (dashboard button): write it now and optionally email it."""
        from types import SimpleNamespace
        from .weekly import send_email, weekly_recap
        acct = self._account()
        shim = SimpleNamespace(journal=self.j, registry=self.reg, cfg=self.cfg, log=logging.getLogger("apexpm.weekly"),
                               state=SimpleNamespace(equity=acct.get("equity")) if acct.get("connected") else None)
        report = weekly_recap(shim, week_end or None)
        sent = False
        if email:
            try:
                sent = send_email(self.cfg, "ApexPM Wallstreet weekly letter (sent by hand)" + (" (new proposals inside)" if "proposes:" in report or "tuned version" in report else ""), report)
            except Exception as exc:  # noqa: BLE001
                # names and the server address only; never the password or the addresses
                return {"ok": True, "emailed": False, "error": type(exc).__name__, "error_text": str(exc)[:200],
                        "smtp": f"{self.cfg.smtp_host}:{self.cfg.smtp_port}", "report": report}
        missing = [k for k, v in (("SMTP_HOST", self.cfg.smtp_host), ("SMTP_USER", self.cfg.smtp_user), ("SMTP_PASS", self.cfg.smtp_pass),
                                  ("EMAIL_TO", self.cfg.email_to)) if not v]   # names only, never values
        return {"ok": True, "emailed": sent, "configured": not missing, "missing": missing, "report": report}

    def plan(self) -> Optional[Dict[str, Any]]:
        r = self.j.q1("SELECT * FROM premarket_plans ORDER BY date DESC, id DESC LIMIT 1")
        if not r:
            return None
        d = dict(r)
        raw = json.loads(d.pop("levels_json"))
        if "day" in raw:
            d["levels"] = raw["day"]
            d["week"] = raw.get("week")
            d["month"] = raw.get("month")
        else:
            d["levels"] = raw
        try:
            d["range"] = json.loads(self.j.get_state(f"range:{d['symbol']}") or "null")
        except Exception:  # noqa: BLE001
            d["range"] = None
        # every symbol's plan for that date, with what the bot knows of today's prices, so the card can
        # show the current price, the previous close (the anchor) and today's open per ticker
        plans = []
        for r2 in self.j.q("SELECT * FROM premarket_plans WHERE date=? ORDER BY symbol", (d["date"],)):
            p = dict(r2)
            raw2 = json.loads(p.pop("levels_json"))
            p["levels"] = raw2.get("day", raw2) if "day" in raw2 else raw2
            p["week"], p["month"] = raw2.get("week"), raw2.get("month")
            try:
                p["range"] = json.loads(self.j.get_state(f"range:{p['symbol']}") or "null")
            except Exception:  # noqa: BLE001
                p["range"] = None
            p["last_price"] = self._last_price(p["symbol"]) or (p["range"] or {}).get("last")
            p["prev_close"] = p["anchor"]
            try:
                p["session_levels"] = json.loads(self.j.get_state(f"session_levels:{p['symbol']}") or "null")
            except Exception:  # noqa: BLE001
                p["session_levels"] = None
            try:
                from . import gex as gexmod
                g = gexmod.cached(self.j, p["symbol"])
                p["gex"] = g if (g and g.get("ok")) else None
                p["gex_text"] = gexmod.lines(g)
            except Exception:  # noqa: BLE001
                p["gex"], p["gex_text"] = None, None
            plans.append(p)
        d["plans"] = plans
        return d

    def settings(self) -> Dict[str, Any]:
        acct = self._account()
        return {"profile": profiles.get_profile(self.j, acct.get("equity") if acct.get("connected") else None),
                "profile_mode": self.j.get_state("risk_profile", "auto") or "auto", "bands": profiles.BANDS,
                "profiles": {k: {kk: vv for kk, vv in v.items()} for k, v in profiles.PROFILES.items()},
                "strategies": self.strategies(), "bot": self.bot()}

    def set_profile(self, name: str, note: str) -> Dict[str, Any]:
        try:
            profiles.set_profile(self.j, name, f"dashboard: {note}" if note else "dashboard")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "profile": name}

    def set_status(self, name: str, status: str, note: str, confirm: str = "") -> Dict[str, Any]:
        """Promotion gate (owner, 2026-11-04). To paper: typed confirmation word + a reason, journaled.
        To live: the server refuses unless the strategy passes the live readiness bar; no override."""
        if name not in self.reg.names():
            return {"ok": False, "error": "unknown_strategy"}
        if status not in ("research", "paper", "live", "suspended", "retired"):
            return {"ok": False, "error": "status_not_allowed_from_dashboard"}
        note = (note or "").strip()
        if status == "paper":
            if confirm.strip().lower() != "promote":
                return {"ok": False, "error": "paper promotion needs the typed word 'promote'"}
            if len(note) < 5:
                return {"ok": False, "error": "paper promotion needs a reason (5+ characters)"}
        if status == "live":
            if confirm.strip().lower() != "go live":
                return {"ok": False, "error": "live promotion needs the typed words 'go live'"}
            r = self.reg.live_readiness(name)
            if not r["ready"]:
                failed = ", ".join(f"{c['name']} ({c['value']})" for c in r["checks"] if not c["ok"])
                return {"ok": False, "error": f"live bar not met: {failed}", "readiness": r}
            if len(note) < 5:
                return {"ok": False, "error": "live promotion needs a reason (5+ characters)"}
        self.reg.set_status(name, status, f"dashboard: {note}" if note else "dashboard")
        return {"ok": True, "status": self.reg.status(name)}


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ApexPM sign in</title><style>body{background:#0b1020;color:#c8d4f0;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#0d0d1a;border:1px solid #1c1c3a;border-radius:14px;padding:24px;width:320px}h1{font-size:16px;letter-spacing:.12em;margin:0 0 12px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #1c1c3a;background:#12122a;color:#fff;margin:6px 0 12px}
button{width:100%;padding:10px;border-radius:8px;border:0;background:#38bdf8;color:#031018;font-weight:700}</style></head>
<body><form onsubmit="location.href='/?token='+encodeURIComponent(document.getElementById('t').value);return false">
<h1>APEXPM WALLSTREET</h1><label>Dashboard token</label><input id="t" type="password" autocomplete="current-password" autofocus>
<button>Sign in</button></form></body></html>"""


def _finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def make_handler(api: Api, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        _set_cookie: Optional[str] = None

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self._cors()
            if self._set_cookie:
                self.send_header("Set-Cookie", self._set_cookie)
                self._set_cookie = None
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            # Browsers reject Infinity/NaN in JSON (a strategy with wins and no losses has an infinite
            # profit factor); send null instead so one such number cannot blank the whole page.
            self._send(code, json.dumps(_finite(obj), default=str).encode())

        def _authed(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token):
                return True
            q = parse_qs(urlparse(self.path).query)
            given = (q.get("token") or [""])[0]
            if given and hmac.compare_digest(given, token):
                self._set_cookie = f"apexpm_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
                return True
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "apexpm_token" and hmac.compare_digest(v, token):
                    return True
            return False

        def _static(self, rel: str, cache: str = "no-store") -> None:
            p = os.path.join(STATIC_DIR, rel)
            if not os.path.isfile(p) or not os.path.abspath(p).startswith(os.path.abspath(STATIC_DIR)):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype, cache)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path in STATIC_FILES:
                return self._static(STATIC_FILES[path])
            if path.startswith("/icons/"):
                return self._static(path.lstrip("/"), cache="public, max-age=86400")
            if path == "/api/health":
                import os as _os
                return self._json({"ok": True, "commit": (_os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or None})
            if not self._authed():
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized"}, 401)
                return self._send(401, LOGIN_HTML.encode(), "text/html; charset=utf-8")
            routes = {
                "/api/overview": api.overview, "/api/bot": api.bot, "/api/positions": api.positions,
                "/api/trades": api.trades, "/api/strategies": api.strategies, "/api/analytics": api.analytics,
                "/api/rejections": api.rejections, "/api/events": api.events, "/api/reports": api.reports, "/api/weekly": api.weekly_reports, "/api/readiness": api.readiness,
                "/api/plan": api.plan, "/api/settings": api.settings, "/api/calendar": api.calendar,
                "/api/decisions": api.decisions, "/api/news": api.news, "/api/variants": api.variants, "/api/audit": api.audit,
                "/api/brain": api.brain, "/api/warden": api.warden, "/api/data": api.data_status, "/api/ledger": api.ledger,
            }
            if path == "/api/backup":
                try:
                    body = api.backup_bytes()
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": str(exc)}, 500)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="apexpm-journal-{et_date()}.db"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return None
            if path == "/api/rejections/rows":
                from urllib.parse import parse_qs
                d = parse_qs(urlparse(self.path).query).get("date", [""])[0]
                return self._json(api.rejection_rows(d or et_date()))
            if path in ("/api/strategy/analytics", "/api/strategy/trades"):
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(self.path).query)
                g = lambda k, d="": qs.get(k, [d])[0]  # noqa: E731
                try:
                    days = max(0, min(int(g("days", "0") or 0), 3650))
                    if path == "/api/strategy/analytics":
                        return self._json(api.strategy_analytics(days=days))
                    return self._json(api.strategy_trades(g("name"), mode=g("mode"), limit=max(1, min(int(g("limit", "500") or 500), 5000)), days=days))
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": str(exc)}, 500)
            if path in routes:
                try:
                    return self._json(routes[path]())
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": str(exc)}, 500)
            if path in ("/", "/index.html", "/app"):
                with open(APP_HTML, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            try:
                return self._do_post()
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("apexpm.dashboard").exception("POST %s failed: %s", self.path, exc)
                return self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}, 500)

        def _do_post(self):
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "bad_json"}, 400)
            if path == "/api/strategy/status":
                return self._json(api.set_status(str(body.get("name", "")), str(body.get("status", "")), str(body.get("note", "")),
                                                 str(body.get("confirm", ""))))
            if path == "/api/settings/profile":
                return self._json(api.set_profile(str(body.get("profile", "")), str(body.get("note", ""))))
            if path == "/api/decisions/resolve":
                return self._json(api.resolve_decision(int(body.get("id", 0)), str(body.get("status", "")), str(body.get("note", ""))))
            if path == "/api/weekly/run":
                return self._json(api.run_weekly(str(body.get("week_end", "")), bool(body.get("email"))))
            if path == "/api/settings/positions_exception":
                return self._json(api.set_positions_exception(bool(body.get("on")), str(body.get("note", ""))))
            if path == "/api/variants/submit":
                return self._json(api.submit_variants(body.get("variants") or [], str(body.get("source", "tuner"))))
            if path == "/api/ledger/record":
                return self._json(api.record_cash(body.get("amount"), str(body.get("kind", "deposit")), str(body.get("note", ""))))
            if path == "/api/weekly/mark_emailed":
                return self._json(api.mark_weekly_emailed(str(body.get("week_end", "")), str(body.get("by", "harness"))))
            if path == "/api/harness/report":
                return self._json(api.harness_report(str(body.get("source", "harness")), body.get("detail") or {}))
            if path == "/api/settings/ladder_reset":
                return self._json(api.ladder_reset(str(body.get("confirm", "")), str(body.get("note", ""))))
            if path == "/api/settings/pause":
                return self._json(api.set_paused(bool(body.get("paused")), str(body.get("note", ""))))
            return self._json({"ok": False, "error": "not_found"}, 404)

        def log_message(self, *_):
            return

    return Handler


def serve(cfg: Config, port: Optional[int] = None, block: bool = True,
          journal: Optional[Journal] = None) -> HTTPServer:
    """Serve the dashboard. Pass the bot's own Journal when running in-process so
    both share one locked connection."""
    port = cfg.dashboard_port if port is None else port      # 0 = any free port (tests)
    bind = cfg.dashboard_bind
    if bind not in ("127.0.0.1", "localhost") and not cfg.dashboard_token:
        raise RuntimeError("refusing to bind the dashboard beyond localhost without DASHBOARD_TOKEN")
    api = Api(cfg, journal or Journal(cfg.db_path))
    # Threaded: a slow request (an 11 MB backup download, a big analytics call) must not stall the
    # platform's 5-second health check or the phone. The journal's own lock serializes database access.
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((bind, port), make_handler(api, cfg.dashboard_token))
    if block:
        print(f"dashboard: http://127.0.0.1:{port}")
        if bind == "0.0.0.0":
            import socket
            try:
                s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s_.connect(("8.8.8.8", 80))
                ip = s_.getsockname()[0]
                s_.close()
            except Exception:  # noqa: BLE001
                ip = "<your-computer-ip>"
            tok = f"?token={cfg.dashboard_token}" if cfg.dashboard_token else ""
            print(f"phone (same Wi-Fi): http://{ip}:{port}/{tok}")
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
