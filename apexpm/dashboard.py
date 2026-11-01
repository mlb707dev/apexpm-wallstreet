"""Local dashboard: JSON API over the journal plus a static mobile-first page.

Binds to 127.0.0.1 only. No secrets are read or written here. Strategy status
changes go through the registry so they are journaled as governance events.
Hosting it anywhere public needs an authenticated API in front of this.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .config import Config
from .db import Journal
from .registry import Registry
from .strategies import ExampleBreakout

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")


def _rows(rows) -> list:
    return [dict(r) for r in rows]


class Api:
    def __init__(self, cfg: Config, journal: Journal):
        self.cfg = cfg
        self.j = journal
        self.reg = Registry(journal, cfg)
        self.reg.register(ExampleBreakout(), "paper")

    def overview(self) -> Dict[str, Any]:
        closed = _rows(self.j.q("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at"))
        open_ = _rows(self.j.q("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY opened_at"))
        rs = [t["rr_realized"] for t in closed if t["rr_realized"] is not None]
        pnl = sum(t["pnl"] or 0 for t in closed)
        wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        cum, peak, dd = 0.0, 0.0, 0.0
        curve = []
        for t in closed:
            cum += t["rr_realized"] or 0
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
            curve.append({"t": t["closed_at"], "r": round(cum, 3), "pnl": t["pnl"]})
        today = _rows(self.j.q("SELECT date, realized_pnl, trades, wins FROM daily_reports ORDER BY date DESC LIMIT 1"))
        return {
            "mode": self.cfg.mode, "paper": self.cfg.paper, "dry_run": self.cfg.dry_run,
            "symbols": self.cfg.symbols,
            "starting_equity": self.j.get_state("starting_equity"),
            "closed": len(closed), "open": len(open_), "wins": wins,
            "win_rate": (wins / len(closed)) if closed else None,
            "expectancy_r": (sum(rs) / len(rs)) if rs else None,
            "total_r": round(sum(rs), 3) if rs else 0.0, "max_dd_r": round(dd, 3),
            "realized_pnl": round(pnl, 2), "curve": curve, "last_report": today[0] if today else None,
        }

    def trades(self, limit: int = 100) -> list:
        return _rows(self.j.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)))

    def strategies(self) -> list:
        out = []
        for n in self.reg.names():
            st = self.reg.stats(n)
            e = self.reg._entries[n]
            out.append({"name": n, "description": e.strategy.description, "status": self.reg.status(n),
                        "mode": self.reg.mode_for(n), "symbols": e.symbols,
                        "stats": st.__dict__ if st else None})
        return out

    def rejections(self, days: int = 7) -> list:
        return _rows(self.j.q(
            "SELECT session_date AS date, setup, reason, COUNT(*) AS n FROM rejections "
            "WHERE ts >= datetime('now', ?) GROUP BY date, setup, reason ORDER BY date DESC, n DESC",
            (f"-{days} days",)))

    def events(self) -> list:
        return [dict(r) for r in self.reg.events(100)]

    def reports(self) -> list:
        return _rows(self.j.q("SELECT * FROM daily_reports ORDER BY date DESC LIMIT 30"))

    def plan(self) -> Optional[Dict[str, Any]]:
        r = self.j.q1("SELECT * FROM premarket_plans ORDER BY date DESC, id DESC LIMIT 1")
        if not r:
            return None
        d = dict(r)
        d["levels"] = json.loads(d.pop("levels_json"))
        return d

    def scans(self, limit: int = 200) -> list:
        return _rows(self.j.q("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)))

    def set_status(self, name: str, status: str, note: str) -> Dict[str, Any]:
        if name not in self.reg.names():
            return {"ok": False, "error": "unknown_strategy"}
        if status not in ("research", "paper", "suspended", "retired"):
            return {"ok": False, "error": "status_not_allowed_from_dashboard"}  # live is CLI + flags only
        self.reg.set_status(name, status, f"dashboard: {note}" if note else "dashboard")
        return {"ok": True, "status": self.reg.status(name)}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode())

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            routes = {
                "/api/overview": api.overview, "/api/trades": api.trades, "/api/strategies": api.strategies,
                "/api/rejections": api.rejections, "/api/events": api.events, "/api/reports": api.reports,
                "/api/plan": api.plan, "/api/scans": api.scans,
            }
            if path in routes:
                try:
                    return self._json(routes[path]())
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": str(exc)}, 500)
            if path in ("/", "/index.html"):
                try:
                    with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    return self._send(404, b"dashboard/index.html missing", "text/plain")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "bad_json"}, 400)
            if path == "/api/strategy/status":
                return self._json(api.set_status(str(body.get("name", "")), str(body.get("status", "")),
                                                 str(body.get("note", ""))))
            return self._json({"ok": False, "error": "not_found"}, 404)

        def log_message(self, *_):
            return

    return Handler


def serve(cfg: Config, port: int = 8788, block: bool = True) -> HTTPServer:
    api = Api(cfg, Journal(cfg.db_path))
    httpd = HTTPServer(("127.0.0.1", port), make_handler(api))
    if block:
        print(f"dashboard: http://127.0.0.1:{port}")
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
