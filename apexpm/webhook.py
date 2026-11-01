"""TradingView webhook receiver. Optional, feature-flagged, stdlib only.

Alerts are hints. The loop recomputes levels itself and records whether the
hint agreed. A payload with a bad secret is dropped and counted.
"""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from .db import Journal, utcnow
from .log import get_logger

log = get_logger()
REQUIRED = ("symbol", "event", "direction", "level", "ts")
EVENTS = ("golden_gate", "trigger_break", "ribbon_fold", "vomy")


def validate(payload: Dict[str, Any], secret: str) -> Optional[str]:
    """Return an error string, or None when the payload is acceptable."""
    if not isinstance(payload, dict):
        return "not_an_object"
    if secret:
        given = str(payload.get("secret", ""))
        if not hmac.compare_digest(given, secret):
            return "bad_secret"
    for k in REQUIRED:
        if k not in payload:
            return f"missing_{k}"
    if payload["event"] not in EVENTS:
        return "unknown_event"
    if payload["direction"] not in ("long", "short"):
        return "bad_direction"
    try:
        float(payload["level"])
    except (TypeError, ValueError):
        return "bad_level"
    return None


class WebhookStore:
    """Journal-backed inbox so hints survive restarts and are auditable."""

    def __init__(self, journal: Journal):
        self.journal = journal
        with journal.tx() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS webhook_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL,
                symbol TEXT NOT NULL, event TEXT NOT NULL, direction TEXT NOT NULL,
                level REAL NOT NULL, price REAL, ribbon_5m TEXT, ribbon_15m TEXT,
                alert_ts TEXT, consumed_at TEXT, error TEXT);
            """)

    def put(self, payload: Dict[str, Any], error: Optional[str]) -> None:
        """Store every payload, valid or not, with its error. Never raises on bad fields."""
        def f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        with self.journal.tx() as c:
            c.execute(
                "INSERT INTO webhook_signals (received_at, symbol, event, direction, level, price, "
                "ribbon_5m, ribbon_15m, alert_ts, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), str(payload.get("symbol", "?"))[:16].upper(), str(payload.get("event", "?"))[:32],
                 str(payload.get("direction", "?"))[:8], f(payload.get("level")) if f(payload.get("level")) is not None else 0.0,
                 f(payload.get("price")), str(payload.get("ribbon_5m", ""))[:8] or None,
                 str(payload.get("ribbon_15m", ""))[:8] or None, str(payload.get("ts", ""))[:40] or None, error))

    def consume(self, symbol: str, direction: str, max_age_min: int = 15) -> Optional[Dict[str, Any]]:
        """Pop the newest unconsumed, valid hint for symbol/direction."""
        row = self.journal.q1(
            "SELECT * FROM webhook_signals WHERE symbol=? AND direction=? AND error IS NULL "
            "AND consumed_at IS NULL AND received_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (symbol.upper(), direction, f"-{max_age_min} minutes"))
        if row is None:
            return None
        with self.journal.tx() as c:
            c.execute("UPDATE webhook_signals SET consumed_at=? WHERE id=?", (utcnow(), row["id"]))
        return dict(row)


class WebhookServer(WebhookStore):
    def __init__(self, cfg, journal: Journal):
        super().__init__(journal)
        self.cfg = cfg
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.cfg.webhook_secret:
            raise RuntimeError("refusing to start webhook without WEBHOOK_SECRET")
        store = self
        secret = self.cfg.webhook_secret
        max_body = self.cfg.webhook_max_body

        class Handler(BaseHTTPRequestHandler):
            timeout = 10  # socket timeout per client

            def do_POST(self):  # noqa: N802
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > max_body:
                    self.send_response(413)
                    self.end_headers()
                    return
                raw = self.rfile.read(n) if n else b""
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    payload, err = {}, "bad_json"
                else:
                    err = validate(payload, secret)
                try:
                    store.put(payload if isinstance(payload, dict) else {}, err)
                except Exception as exc:  # noqa: BLE001
                    log.warning("webhook store failed: %s", exc)
                self.send_response(400 if err else 200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": err is None, "error": err}).encode())

            def log_message(self, *_):  # silence default stderr logging
                return

        self._httpd = HTTPServer((self.cfg.webhook_bind, self.cfg.webhook_port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("webhook listening on %s:%d", self.cfg.webhook_bind, self.cfg.webhook_port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
