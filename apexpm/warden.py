"""Warden: the role that checks every other role did its work, and does it for them when it can.

Duties it patrols (each with the evidence it looks for, and the repair it is allowed to run):

  bot alive              heartbeat fresh                                   no repair (it IS the bot)
  strategies scanned     on a session day: from 10:30 ET the scanner has journaled looks (a rejection or
                         a trade) today; from 15:30 ET every regular-hours strategy has at least one.
                         24/5 desk strategies are skipped (they scan in their own sessions; the overnight
                         desk duty covers them). A strategy only journals when a setup gets close, so a
                         quiet morning is normal (one strategy's first look on 2026-11-09 came at 2:39 PM).
                                                                            no repair; flagged
  postmortems            no closed trade older than an hour without a review   repair: replay them
  brain                  thought since the newest review                        repair: think
  auditor                audited within 24 hours                                repair: audit
  daily review           the last session day with scans has its report         repair: write it
  weekly recap           the last Friday's recap exists by Saturday              repair: write it
  nightly tuner          the nightly pass reported within 36 hours
                         (Tuesday to Saturday)                                  no repair; flagged
  journal healthy        SQLite quick_check passes                               no repair; flagged
  journal backed up      a backup was pulled within 36 hours       no repair; flagged

A duty that still fails after its repair raises one SYSTEM ALERT per duty per day (shown at the top of
the Overview and in the daily review), so a silent harness cannot stay silent. Job failures are not owner
decisions (owner, 2026-11-10): they never go to "Needs your call"; they are fixed in the build. The warden never trades, never promotes, never changes settings.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PATROL_EVERY_MIN = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _event_within(journal, name: str, event: str, hours: float) -> Optional[str]:
    r = journal.q1("SELECT ts FROM strategy_events WHERE name=? AND event=? ORDER BY id DESC LIMIT 1", (name, event))
    if not r:
        return None
    try:
        ts = datetime.fromisoformat(r["ts"])
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return r["ts"] if _now() - ts <= timedelta(hours=hours) else None


def _last_session_with_scans(journal) -> Optional[str]:
    r = journal.q1("SELECT session_date FROM scans WHERE session_date IS NOT NULL ORDER BY id DESC LIMIT 1")
    return r["session_date"] if r else None


def duties(journal, registry, cfg, bot=None) -> List[Dict[str, Any]]:
    """Each duty: name, check() -> (ok, detail), repair() or None."""
    try:
        from . import postmortem as pm
    except ImportError:          # the public build ships the warden without the replay and brain roles
        pm = None
    now_et = _now().astimezone(ET)
    today = now_et.date().isoformat()
    out: List[Dict[str, Any]] = []

    def heartbeat():
        hb = journal.get_state("heartbeat")
        if not hb:
            return False, "no heartbeat yet"
        age = (_now() - datetime.fromisoformat(hb)).total_seconds()
        return age < 600, f"last heartbeat {int(age)}s ago"
    out.append({"name": "bot alive", "check": heartbeat, "repair": None})

    def scanned():
        if now_et.weekday() >= 5 or journal.get_state("market") != "open" or (now_et.hour, now_et.minute) < (10, 30):
            return True, "not a session hour"
        try:
            from .overnight import is_desk_strategy
        except ImportError:  # noqa: BLE001
            is_desk_strategy = lambda s: False  # noqa: E731
        entries = getattr(registry, "_entries", {})
        rth_names = [n for n in registry.names() if registry.status(n) != "retired"
                     and not (n in entries and is_desk_strategy(entries[n].strategy))]
        looked = lambda n: bool(journal.q1("SELECT 1 FROM rejections WHERE session_date=? AND setup=? LIMIT 1", (today, n)) or  # noqa: E731
                                journal.q1("SELECT 1 FROM trades WHERE session_date=? AND setup=? LIMIT 1", (today, n)))
        seen = [n for n in rth_names if looked(n)]
        if rth_names and not seen:
            return False, "the scanner has journaled no look at all today"
        if (now_et.hour, now_et.minute) < (15, 30):
            return True, f"scanning: {len(seen)} of {len(rth_names)} regular-hours strategies have looked so far"
        missing = [n for n in rth_names if n not in seen]
        return not missing, ("every regular-hours strategy has looked at today's market" if not missing
                             else "no look journaled all session from: " + ", ".join(missing))
    out.append({"name": "strategies scanned", "check": scanned, "repair": None})

    def postmortems():
        if pm is None:
            return True, "replay role not installed"
        p = pm.pending(journal, min_age_min=60, limit=500)
        return not p, (f"{len(p)} closed trades not replayed yet" if p else "every closed trade is replayed")
    feed = getattr(bot, "feed", None)
    out.append({"name": "postmortems", "check": postmortems,
                "repair": (lambda: pm.backfill(journal, feed, limit=200)) if feed is not None else None})

    def brain_fresh():
        if pm is None:
            return True, "brain role not installed"
        newest = journal.q1("SELECT MAX(ts) AS ts FROM trade_reviews")
        th = journal.q1("SELECT ts FROM strategy_events WHERE name='brain' AND event='think' ORDER BY id DESC LIMIT 1")
        if not newest or not newest["ts"]:
            return True, "nothing to learn from yet"
        if not th:
            return False, "the brain has never run"
        return th["ts"] >= newest["ts"], ("lessons are current" if th["ts"] >= newest["ts"] else "new replays since the brain last thought")

    def brain_repair():
        from . import brain
        return brain.think(journal, registry, cfg, data=None)
    out.append({"name": "brain", "check": brain_fresh, "repair": brain_repair})

    def audited():
        ts = _event_within(journal, "auditor", "audit", 24)
        return bool(ts), (f"last audit {ts}" if ts else "no audit in 24 hours")

    def audit_repair():
        from .auditor import audit
        return audit(journal, registry, cfg)
    out.append({"name": "auditor", "check": audited, "repair": audit_repair})

    def reviewed():
        last = _last_session_with_scans(journal)
        if not last:
            return True, "no session yet"
        if last == today and (now_et.weekday() >= 5 or now_et.hour < 16):
            return True, "today's session is not over"
        has = journal.q1("SELECT 1 FROM daily_reports WHERE date=?", (last,))
        return bool(has), (f"report for {last} written" if has else f"no daily report for {last}")

    def review_repair():
        if bot is None:
            return None
        from .review import daily_review
        last = _last_session_with_scans(journal)
        daily_review(bot, last)
        journal.set_state("reviewed", last)
        return {"written": last}
    out.append({"name": "daily review", "check": reviewed, "repair": review_repair if bot is not None else None})

    def weekly():
        # the most recent Friday whose recap window has passed (Friday 20:30 ET)
        d = now_et.date()
        back = (d.weekday() - 4) % 7
        friday = d - timedelta(days=back)
        if back == 0 and (now_et.hour, now_et.minute) < (20, 30):
            friday -= timedelta(days=7)
        if not journal.q1("SELECT 1 FROM scans WHERE session_date<=? AND session_date>? LIMIT 1", (friday.isoformat(), (friday - timedelta(days=7)).isoformat())):
            return True, "no sessions that week"
        done = journal.get_state("weekly_recap_done") == friday.isoformat()
        if not done:
            try:
                done = bool(journal.q1("SELECT 1 FROM weekly_reports WHERE week_end=?", (friday.isoformat(),)))
            except Exception:  # noqa: BLE001
                done = False
        return bool(done), (f"recap for week ending {friday} exists" if done else f"no recap for week ending {friday}")

    def weekly_repair():
        if bot is None:
            return None
        from .weekly import weekly_recap
        d = now_et.date()
        back = (d.weekday() - 4) % 7
        friday = d - timedelta(days=back)
        if back == 0 and (now_et.hour, now_et.minute) < (20, 30):
            friday -= timedelta(days=7)
        weekly_recap(bot, friday.isoformat())
        journal.set_state("weekly_recap_done", friday.isoformat())
        return {"written": friday.isoformat()}
    out.append({"name": "weekly recap", "check": weekly, "repair": weekly_repair if bot is not None else None})

    def evolved():
        if now_et.weekday() in (0, 6):          # Sunday/Monday mornings follow a weekend with no pass expected
            return True, "no pass expected after the weekend"
        ts = _event_within(journal, "evolve", "ran", 36)
        return bool(ts), (f"nightly pass reported {ts}" if ts else "no nightly tuner report in 36 hours (the nightly job did not run or could not reach the bot)")
    out.append({"name": "nightly tuner", "check": evolved, "repair": None})

    def healthy():
        h = journal.health()
        return h["ok"], (f"journal {h['size_bytes'] // 1024} KB, {h['counts'].get('trades', 0)} trades, integrity {h['check']}")
    out.append({"name": "journal healthy", "check": healthy, "repair": None})

    def public_clean():
        if now_et.weekday() in (0, 6):
            return True, "no check expected after the weekend"
        r = journal.q1("SELECT ts, detail FROM strategy_events WHERE name='privacy' AND event='ran' ORDER BY id DESC LIMIT 1")
        if not r:
            return False, "the public-repo privacy check has not reported yet"
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            d = {}
        if not _event_within(journal, "privacy", "ran", 36):
            return False, f"no public-repo privacy check in 36 hours (last {r['ts']})"
        if d.get("ok"):
            return True, f"public repo clean as of {r['ts']}"
        parts = []
        if d.get("stray_branches"):
            parts.append("extra branches: " + ", ".join(d["stray_branches"]))
        if d.get("findings"):
            parts.append(f"{len(d['findings'])} file version(s) with private terms or secrets")
        return False, "public repo needs attention: " + "; ".join(parts or ["see logs/privacy.log"])
    out.append({"name": "public repo clean", "check": public_clean, "repair": None})

    def backed_up():
        if now_et.weekday() in (0, 6):
            return True, "no backup expected after the weekend"
        ts = _event_within(journal, "backup", "ran", 36)
        return bool(ts), (f"last backup pulled {ts}" if ts else "no backup pulled in 36 hours (the nightly job did not run or could not reach the bot)")
    out.append({"name": "journal backed up", "check": backed_up, "repair": None})

    def overnight_desk():
        """24/5 desk: off by default (fine); when on, it must have ticked recently while an extended session is open,
        and any 24/5 strategy that passed its rule must have an activation decision waiting for the owner."""
        from . import ext_sessions as xs
        from .overnight import readiness, propose_ready
        ready = readiness(journal, registry)
        if ready:
            propose_ready(journal, registry)
        tail = f"; {len(ready)} 24/5 strateg{'y' if len(ready) == 1 else 'ies'} ready for review" if ready else ""
        if not getattr(cfg, "overnight_enabled", False):
            return True, "overnight desk off (OVERNIGHT_ENABLED=false); 24/5 strategies stay in the lab" + tail
        sess = xs.session_at(now_et)
        if sess not in xs.EXT_SESSIONS:
            return True, f"overnight desk on; {sess} session" + tail
        ts = journal.get_state("overnight_tick")
        if not ts:
            return False, "overnight desk is on but has never ticked"
        age = (_now() - datetime.fromisoformat(ts)).total_seconds() / 60
        return age <= 30, (f"overnight desk ticked {age:.0f} min ago ({sess})" if age <= 30 else f"overnight desk silent for {age:.0f} min during {sess}") + tail
    out.append({"name": "overnight desk (24/5)", "check": overnight_desk, "repair": None})
    return out


def patrol(journal, registry, cfg, bot=None, repair: bool = True) -> Dict[str, Any]:
    """Check every duty; run the repair for a failing one; re-check; record; escalate what still fails."""
    results = []
    today = _now().astimezone(ET).date().isoformat()
    for d in duties(journal, registry, cfg, bot):
        try:
            ok, detail = d["check"]()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"check failed: {exc}"
        repaired = None
        if not ok and repair and d["repair"] is not None:
            try:
                d["repair"]()
                ok2, detail2 = d["check"]()
                repaired = ok2
                if ok2:
                    detail = f"was: {detail}; repaired by the warden"
                    ok = True
                else:
                    detail = f"{detail}; repair did not clear it ({detail2})"
            except Exception as exc:  # noqa: BLE001
                repaired = False
                detail = f"{detail}; repair failed: {exc}"
        results.append({"duty": d["name"], "ok": bool(ok), "detail": detail, "repaired": repaired, "can_repair": d["repair"] is not None})
        if not ok and repair:
            key = f"warden_alerted:{d['name']}:{today}"
            if not journal.get_state(key):
                try:   # a system alert, never an owner decision
                    with journal.tx() as c:
                        c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?, 'warden', 'alert', ?)",
                                  (_iso(_now()), json.dumps({"duty": d["name"], "detail": detail,
                                                             "check": "(the bot's logs, the nightly tuner's scheduler)"})))
                    journal.set_state(key, _iso(_now()))
                except Exception:  # noqa: BLE001
                    pass
    out = {"ts": _iso(_now()), "ok": all(r["ok"] for r in results), "checks": results}
    journal.set_state("warden_last", json.dumps(out))
    try:
        with journal.tx() as c:
            c.execute("INSERT INTO strategy_events (ts, name, event, detail) VALUES (?, 'warden', 'patrol', ?)",
                      (out["ts"], json.dumps({"ok": out["ok"], "failed": [r["duty"] for r in results if not r["ok"]],
                                              "repaired": [r["duty"] for r in results if r["repaired"]]})))
    except Exception:  # noqa: BLE001
        pass
    return out


def last(journal) -> Optional[Dict[str, Any]]:
    raw = journal.get_state("warden_last")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def due(journal) -> bool:
    l = last(journal)
    if not l:
        return True
    try:
        return _now() - datetime.fromisoformat(l["ts"]) >= timedelta(minutes=PATROL_EVERY_MIN)
    except (KeyError, ValueError):
        return True


def warden_lines(w: Optional[Dict[str, Any]]) -> List[str]:
    lines = ["## Warden (is every role doing its job)", ""]
    if not w:
        return lines + ["- no patrol yet"]
    for c in w["checks"]:
        lines.append(f"- {'OK  ' if c['ok'] else 'FAIL'} {c['duty']}: {c['detail']}")
    return lines
