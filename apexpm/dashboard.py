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
import mimetypes
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import calendar as cal
from . import profiles
from .config import Config
from .db import Journal, et_date
from .registry import Registry
from .strategies import ExampleBreakout

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
STATIC_FILES = {"/manifest.json": "manifest.json", "/sw.js": "sw.js"}


def _rows(rows) -> list:
    return [dict(r) for r in rows]


class Api:
    def __init__(self, cfg: Config, journal: Journal):
        self.cfg = cfg
        self.j = journal
        self.reg = Registry(journal, cfg)
        self.reg.register(ExampleBreakout(), "paper")
        cal.ensure_table(journal)
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
        cum, out = 0.0, []
        for t in closed:
            cum += t["rr_realized"] or 0
            out.append({"t": t["closed_at"], "r": round(cum, 3), "pnl": t["pnl"], "setup": t["setup"]})
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
        cap = self.cfg.max_daily_loss
        if self.cfg.max_daily_loss_pct > 0 and start:
            cap = min(cap, float(start) * self.cfg.max_daily_loss_pct)
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
            "today": {"date": today, "realized_pnl": round(day_pnl, 2), "trades": trades_today,
                      "loss_budget_left": round(max(0.0, cap + day_pnl), 2),
                      "max_daily_loss": round(cap, 2),
                      "open_positions": sum(1 for t in open_ if t["status"] == "open"),
                      "pending": sum(1 for t in open_ if t["status"] == "pending"),
                      "max_positions": self.cfg.max_positions, "in_open_notional": round(in_open, 2),
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
        prof = profiles.get_profile(self.j)
        eff = profiles.EffectiveConfig(self.cfg, prof)
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
                          "kelly_fraction": eff.kelly_fraction, "max_positions": self.cfg.max_positions,
                          "max_daily_loss": self.cfg.max_daily_loss, "max_daily_loss_pct": self.cfg.max_daily_loss_pct,
                          "kill_dd_pct": self.cfg.kill_dd_pct,
                          "stop_cooldown_min": self.cfg.stop_cooldown_min},
            "halted": self.j.get_state(f"halted:{et_date()}") == "1",
            "entries_paused": profiles.entries_paused(self.j),
            "key_fingerprint": (self.cfg.alpaca_api_key[:4] + "…") if self.cfg.alpaca_api_key else None,
            "stream": {"ticks": int(self.j.get_state("stream_ticks") or 0), "last_tick": self.j.get_state("stream_last_tick"),
                       "feed": self.cfg.data_feed, "enabled": self.cfg.stream_enabled},
            "day": self._day(),
            "alerts": self._alerts(),
            "volume": self.volume(),
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
        return _rows(self.j.q("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)))

    def strategies(self) -> list:
        out = []
        for n in self.reg.names():
            st = self.reg.stats(n)
            e = self.reg._entries[n]
            out.append({"name": n, "description": e.strategy.description, "status": self.reg.status(n),
                        "book": self.reg.book(n), "label": self.reg.label(n),
                        "mode": self.reg.mode_for(n), "symbols": e.symbols, "stats": st.__dict__ if st else None})
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

        return {"by_strategy": agg(lambda t: t["setup"]), "by_direction": agg(lambda t: t["direction"]),
                "by_hour": agg(hour), "by_weekday": agg(dow), "by_exit": agg(lambda t: t["exit_reason"]),
                "by_regime": agg(lambda t: t["regime"]), "n": len(closed)}

    def rejections(self, days: int = 7) -> list:
        return _rows(self.j.q(
            "SELECT session_date AS date, setup, reason, COUNT(*) AS n FROM rejections "
            "WHERE ts >= datetime('now', ?) GROUP BY date, setup, reason ORDER BY date DESC, n DESC", (f"-{days} days",)))

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

    def settings(self) -> Dict[str, Any]:
        return {"profile": profiles.get_profile(self.j),
                "profiles": {k: {kk: vv for kk, vv in v.items()} for k, v in profiles.PROFILES.items()},
                "strategies": self.strategies(), "bot": self.bot()}

    def set_profile(self, name: str, note: str) -> Dict[str, Any]:
        try:
            profiles.set_profile(self.j, name, f"dashboard: {note}" if note else "dashboard")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "profile": name}

    def set_status(self, name: str, status: str, note: str) -> Dict[str, Any]:
        if name not in self.reg.names():
            return {"ok": False, "error": "unknown_strategy"}
        if status not in ("research", "paper", "suspended", "retired"):
            return {"ok": False, "error": "status_not_allowed_from_dashboard"}
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
            self._send(code, json.dumps(obj, default=str).encode())

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
                return self._json({"ok": True})
            if not self._authed():
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized"}, 401)
                return self._send(401, LOGIN_HTML.encode(), "text/html; charset=utf-8")
            routes = {
                "/api/overview": api.overview, "/api/bot": api.bot, "/api/positions": api.positions,
                "/api/trades": api.trades, "/api/strategies": api.strategies, "/api/analytics": api.analytics,
                "/api/rejections": api.rejections, "/api/events": api.events, "/api/reports": api.reports,
                "/api/plan": api.plan, "/api/settings": api.settings, "/api/calendar": api.calendar,
                "/api/decisions": api.decisions,
            }
            if path in routes:
                try:
                    return self._json(routes[path]())
                except Exception as exc:  # noqa: BLE001
                    return self._json({"error": str(exc)}, 500)
            if path in ("/", "/index.html"):
                return self._static("index.html")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            except json.JSONDecodeError:
                return self._json({"ok": False, "error": "bad_json"}, 400)
            if path == "/api/strategy/status":
                return self._json(api.set_status(str(body.get("name", "")), str(body.get("status", "")), str(body.get("note", ""))))
            if path == "/api/settings/profile":
                return self._json(api.set_profile(str(body.get("profile", "")), str(body.get("note", ""))))
            if path == "/api/decisions/resolve":
                return self._json(api.resolve_decision(int(body.get("id", 0)), str(body.get("status", "")), str(body.get("note", ""))))
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
    port = port or cfg.dashboard_port
    bind = cfg.dashboard_bind
    if bind not in ("127.0.0.1", "localhost") and not cfg.dashboard_token:
        raise RuntimeError("refusing to bind the dashboard beyond localhost without DASHBOARD_TOKEN")
    api = Api(cfg, journal or Journal(cfg.db_path))
    httpd = HTTPServer((bind, port), make_handler(api, cfg.dashboard_token))
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
                ip = "<your-mac-ip>"
            tok = f"?token={cfg.dashboard_token}" if cfg.dashboard_token else ""
            print(f"phone (same Wi-Fi): http://{ip}:{port}/{tok}")
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
