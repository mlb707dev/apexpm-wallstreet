# ApexPM Wallstreet

A paper-first, governance-driven trading agent framework for US stocks on
Alpaca, with an options strike-selection helper ready for an options module. Multiple strategies run under one registry with shared risk, a shared
journal, automatic suspension when a strategy loses its edge, and an honest
backtester that decides on one bar and fills on the next.

This is the public framework. The strategies, parameters and research that
run in the private build are not included; an example strategy shows how the
pieces fit.

## What it does

- Computes a daily level map (prior close ± ATR bands) and an EMA stack on
  intraday bars from Alpaca market data.
- Asks each registered strategy for a signal. A signal carries its entry,
  stop, target, max hold and the level it is aiming for. Strategies never
  manage exits and never talk to the broker.
- Runs every signal through a risk layer that cannot be bypassed: kill switch
  on total equity, daily loss flip to dry-run, max trades and positions,
  duplicate guard, stop cooldown, no-entry and flat-by cutoffs.
- Sizes with fractional Kelly scaled by volatility, capped by notional
  limits, with fractional shares when the account is small.
- Journals every scan, rejection, order attempt and fill to SQLite, intent
  row first, then the order, then the fill.
- Ships a daily review command (`--review`) that computes rolling expectancy,
  profit factor, drawdown and level discipline per strategy and suspends any
  that fall below the bar. Nothing un-suspends itself.
- Serves a mobile-first dashboard off the real journal.

## Architecture

```
                     market data (Alpaca)            TradingView alert (optional)
                             │                                  │
                             ▼                                  ▼
                   ┌──────────────────┐               ┌──────────────────┐
                   │ levels + EMA     │               │ webhook inbox    │
                   │ stack per symbol │               │ (secret-checked) │
                   └────────┬─────────┘               └────────┬─────────┘
                            │ MarketSnapshot                   │ hint only
                            ▼                                  │
      ┌──────────────────────────────────────────┐             │
      │ strategy registry                        │◄────────────┘
      │  research → paper → live → suspended     │
      │  each strategy: evaluate(snapshot) → Signal
      └────────────────────┬─────────────────────┘
                           │ Signal (entry, stop, target, hold, level)
                           ▼
      ┌──────────────────────────────────────────┐
      │ risk layer (always on)                   │
      │  kill switch · daily loss · limits · cooldown
      │  sizing: fractional Kelly × vol target   │
      └────────────────────┬─────────────────────┘
                           │ journal intent row FIRST
                           ▼
      ┌─────────────┐   order   ┌─────────────┐   poll   ┌──────────────┐
      │ SQLite      │◄──────────│ broker      │─────────►│ pending →    │
      │ journal     │           │ (paper/live;│          │ open/cancel  │
      │             │           │  DRY_RUN    │          └──────────────┘
      │ trades      │           │  never      │
      │ rejections  │           │  orders)    │   exit checker compares price
      │ scans       │           └─────────────┘   to the STORED plan only
      │ events      │                                     │
      └──────┬──────┘◄────────────────────────────────────┘
             │
             ├──► daily review ──► auto-suspension ──► registry (learning loop)
             │
             └──► dashboard (127.0.0.1) · backtester (same Strategy objects)
```

The same `Strategy` objects run in the live loop and in the backtester, so a
strategy is tested on exactly the code that trades it.

## Safeguards

- `PAPER=true` and `DRY_RUN=true` are the defaults. In dry-run the broker
  wrapper refuses every order, independently of the loop.
- A strategy can only order if its registry status allows it **and** the
  global flags allow it. Live requires status `live`, `PAPER=false` and
  `DRY_RUN=false`, and prints a 10-second abort warning.
- The kill-switch anchor and the halted flag persist per session date, so a
  restart cannot reset them.
- Order lifecycle: intent row → order → poll → promote on fill or cancel if
  unfilled. Exits close the broker's real position and record its fill.
- Time-based exits (max hold, flat-by) fire even when no price is available.
- Webhook: secret required, body capped, bound to localhost, every payload
  stored with its validation error.
- Secrets live only in `.env`. The config object never prints them.

## Research harness

- `apexpm/backtest.py` runs any strategy on daily, 5-minute and 15-minute
  bars with honest timing (decide at the close of bar t, fill on t+1 or later),
  a resting-limit or next-open fill model, slippage and commission, an
  entry-bar stop check, and no re-entry on an exit bar.
- `scripts/fetch_alpaca_history.py` pulls years of intraday bars.
- Promotion from research to paper requires a study file; the rules are in
  `GOVERNANCE.md`.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your Alpaca PAPER keys
.venv/bin/python -m apexpm --check
.venv/bin/python -m apexpm --levels
.venv/bin/python -m apexpm --once
.venv/bin/python -m apexpm --dashboard    # http://127.0.0.1:8788
.venv/bin/python -m pytest
```

## How I built it

I specified the system and owned the decisions: the strategy framework, the risk rules that cannot be bypassed, the governance rules for promoting a strategy from research to paper to live, and the trading logic itself. I ran it as a spec-driven build with Claude Code handling implementation under my review, and I made every promotion call. I had the code put through independent review passes before trusting it, and those caught two defects that shaped the framework: a same-bar look-ahead in an early backtester, and a paper-order path that assumed fills. Both are now covered by tests.

## Disclaimer

This is research software. It is not financial advice and it will lose money
if you point it at a strategy without an edge. Paper trade first. Nothing
here is a recommendation to trade.

## License

MIT. Built by [mlb707dev](https://github.com/mlb707dev).
