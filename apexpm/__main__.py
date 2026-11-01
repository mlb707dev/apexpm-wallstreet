"""python -m apexpm [--once] [--levels]"""
from __future__ import annotations

import argparse
import sys

from .config import load
from .loop import Bot


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="apexpm")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--levels", action="store_true", help="print today's ATR levels and exit")
    ap.add_argument("--check", action="store_true", help="validate config and connectivity")
    ap.add_argument("--review", action="store_true", help="run the daily review and print the report")
    ap.add_argument("--status", action="store_true", help="show strategy statuses and rolling stats")
    ap.add_argument("--dashboard", action="store_true", help="serve the local dashboard on 127.0.0.1:8788")
    ap.add_argument("--set-status", nargs=2, metavar=("STRATEGY", "STATUS"), help="manually set a strategy status")
    args = ap.parse_args(argv)

    cfg = load()
    bot = Bot(cfg)

    if args.check:
        problems = cfg.validate()
        print(f"mode: {cfg.mode}")
        print(f"broker: {'connected' if bot.broker.connected else 'offline (no keys)'}")
        for p in problems:
            print(f"problem: {p}")
        if cfg.alpaca_api_key.startswith("your_") or cfg.alpaca_secret_key.startswith("your_"):
            print("problem: .env still contains the placeholder keys (file not saved?)")
            return 1
        if bot.broker.connected:
            try:
                a = bot.broker.account()
                print(f"alpaca: OK ({'paper' if cfg.paper else 'LIVE'})  equity: {a.equity:.2f}  cash: {a.cash:.2f}")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "unauthorized" in msg.lower() or "401" in msg:
                    print("problem: Alpaca rejected the keys (401). Check both values and that PAPER matches the key type.")
                else:
                    print(f"problem: Alpaca error: {msg[:200]}")
                return 1
        return 1 if problems else 0

    if args.dashboard:
        from .dashboard import serve
        serve(cfg)
        return 0

    if args.review:
        from .review import daily_review
        print(daily_review(bot))
        return 0

    if args.status:
        for n in bot.registry.names():
            st = bot.registry.stats(n)
            line = f"{n:<18} {bot.registry.status(n):<10} mode={bot.registry.mode_for(n):<8}"
            if st:
                line += f" trades={st.trades} win={st.win_rate:.0%} exp={st.expectancy_r:+.2f}R pf={st.profit_factor:.2f} dd={st.max_dd_r:.1f}R disc={st.level_discipline:.0%}"
            print(line)
        return 0

    if args.set_status:
        name, status = args.set_status
        if name not in bot.registry.names():
            print(f"unknown strategy {name}; known: {bot.registry.names()}")
            return 1
        if status == "live":
            ans = input(f"Set {name} to LIVE? This allows real orders once PAPER=false and DRY_RUN=false. Type 'yes' to confirm: ")
            if ans.strip().lower() != "yes":
                print("aborted")
                return 1
            note = input("Evidence note (required): ").strip()
            if not note:
                print("aborted: evidence note required")
                return 1
            bot.registry.set_status(name, status, f"manual via CLI: {note}")
        else:
            bot.registry.set_status(name, status, "manual via CLI")
        print(f"{name} -> {status}")
        return 0

    if args.levels:
        from datetime import datetime, timezone
        from .loop import ET
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        for s in cfg.symbols:
            lv = bot._levels_for(s, today)
            if lv is None:
                print(f"{s}: no data (feed offline?)")
                continue
            print(f"{s}  anchor={lv.anchor:.2f}  atr={lv.atr:.2f}")
            for k in sorted(lv.up):
                print(f"   +{k:<6} {lv.up[k]:>9.2f}    -{k:<6} {lv.down[k]:>9.2f}")
        return 0

    if args.once:
        bot.startup()
        bot.check_exits()
        sigs = bot.scan_once()
        print(f"signals: {len(sigs)}")
        return 0

    bot.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
