"""Nightly per-strategy tuning: explore new parameter combos, select challengers, submit to the bot.

Usage:
  .venv/bin/python scripts/tune.py                      # all bases, 12 new combos each, no submit
  .venv/bin/python scripts/tune.py --n 24 --submit      # and POST challengers to the live dashboard
  .venv/bin/python scripts/tune.py --bases <base1>,<base2> --symbols SPY,QQQ
Results accumulate in data/tuning/results.jsonl; the study lands in research/studies/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apexpm import tuner  # noqa: E402
from apexpm.config import Config  # noqa: E402
from apexpm.strategies.spaces import BASES  # noqa: E402


def submit(specs, url, token):
    req = urllib.request.Request(url.rstrip("/") + "/api/variants/submit", data=json.dumps({"variants": specs}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default=",".join(BASES))
    ap.add_argument("--symbols", default="")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=int(date.today().strftime("%Y%m%d")))
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--url", default=os.environ.get("APEXPM_URL", ""))
    ap.add_argument("--long-only", default="auto", choices=["auto", "yes", "no"],
                    help="auto: ask the broker whether the account can short (cash accounts cannot)")
    a = ap.parse_args()
    cfg = Config()
    symbols = [s for s in (a.symbols or ",".join(cfg.symbols)).split(",") if s]
    bases = [b for b in a.bases.split(",") if b]
    long_only = {"yes": True, "no": False}.get(a.long_only)
    if long_only is None:
        try:
            from apexpm.broker import Broker
            long_only = not Broker(cfg).account().shorting_enabled
        except Exception:  # noqa: BLE001
            long_only = True          # offline: assume the small-account default (no shorting)
    print(f"long-only search: {long_only}")
    results = tuner.run(bases, symbols, a.n, a.seed, a.workers, long_only=long_only)
    when = date.today().isoformat()
    md = tuner.report_md(results, symbols, when)
    os.makedirs("research/studies", exist_ok=True)
    path = f"research/studies/tuning-{when}.md"
    open(path, "w").write(md)
    print(md)
    print("written:", path)
    specs = [tuner.to_spec(r["challenger"], f"tuner {when}") for r in results.values() if r["challenger"]]
    if specs and a.submit:
        if not a.url or not cfg.dashboard_token:
            print("submit skipped: APEXPM_URL / DASHBOARD_TOKEN missing")
        else:
            print("submit:", json.dumps(submit(specs, a.url, cfg.dashboard_token)))
    elif specs:
        print(f"{len(specs)} challenger(s) found; run with --submit to propose them")


if __name__ == "__main__":
    main()
