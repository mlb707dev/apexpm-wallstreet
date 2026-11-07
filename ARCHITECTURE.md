# Architecture

Revised 2026-11-01 after the owner's review. The system is organized as a
flow of books, a journal that outlives any broker account, and loops that
judge and correct.

```
                 ┌──────────────── MARKET ────────────────┐
                 │ Alpaca bars · option chains · calendar │
                 └───────────────────┬────────────────────┘
                                     ▼
                      snapshot: levels, EMA stacks, volume,
                      day context (events, policy), regime
                                     ▼
          ┌──────────────── STRATEGY LAB (shadow book) ────────────────┐
          │ every registered strategy evaluates every scan               │
          │ lab + quarantine strategies: signals journaled, never sent  │
          └───────────────────┬─────────────────────────────────────────┘
                              │ Signal: entry · stop · target · hold · level · confluence
                              ▼
          ┌──────────────── RISK LAYER (always, every book) ────────────┐
          │ kill switch (total equity) · daily loss % · heat · cooldown │
          │ calendar policy (size, windows, skips) · profile ceilings   │
          │ sizing: fractional Kelly × vol target × confluence, capped  │
          └───────────────────┬─────────────────────────────────────────┘
                              ▼
      ┌─────────────┐   ┌─────────────┐   ┌──────────────┐
      │  LIVE book  │   │ PAPER book  │   │ SHADOW book  │
      │ real orders │   │ paper orders│   │ no orders    │
      └──────┬──────┘   └──────┬──────┘   └──────┬───────┘
             └─────────────────┼─────────────────┘
                               ▼
          ┌──────────────── JOURNAL (SQLite on persistent disk) ────────┐
          │ trades (with book, initial stop, exit plan history)         │
          │ rejections · scans · events · decisions · calendar          │
          │ survives paper resets: equity anchors re-seed, rows persist │
          └───────────────────┬─────────────────────────────────────────┘
                              ▼
          ┌──────────────── ANALYTICS ──────────────────────────────────┐
          │ per strategy · per book · by hour/day/regime/exit · R curves│
          └───────────────────┬─────────────────────────────────────────┘
                              ▼
          ┌──────────────── REVIEW & JUDGMENT ──────────────────────────┐
          │ daily after the close, automatically:                       │
          │  obvious → acts: quarantine on loss streak, negative         │
          │            expectancy, PF < 1, drawdown, poor discipline    │
          │  borderline → DECISIONS queue for the owner (approve/dismiss)│
          └───────────────────┬─────────────────────────────────────────┘
                              ▼
          ┌──────────────── LIFECYCLE ──────────────────────────────────┐
          │ lab → paper → live (manual, evidence-backed promotions)     │
          │ any → quarantine (automatic) → lab (tuning) → paper again   │
          └───────────────────┬─────────────────────────────────────────┘
                              ▼
          ┌──────────────── INDEPENDENT AUDIT ──────────────────────────┐
          │ daily light / weekly deep: code, strategy logic, process,   │
          │ limits. Patches the obvious; surfaces the rest as decisions │
          └─────────────────────────────────────────────────────────────┘

   Owner surfaces: mobile app (books, bot, calendar, decisions, pause,
   tighten) · daily report · research vault
```

## Books

| Book | Who is in it | Orders | Purpose |
|---|---|---|---|
| live | status `live` | real | compounding capital |
| paper | status `paper` | Alpaca paper | last proving ground before live |
| shadow | status `research` (lab) and `suspended` (quarantine) | none | every signal journaled so the lab sees what it would have done |

A strategy's book is a function of its status; the risk layer runs the same
gates on all three. The journal records the book on every row, so analytics
can compare a strategy's shadow performance to its paper performance before a
promotion.

## Lifecycle rules (automatic)

- `SUSPEND_LOSS_STREAK` (default 6) consecutive losses → quarantine, checked
  before any minimum-trades gate.
- With at least `REVIEW_MIN_TRADES` closed: expectancy below floor, profit
  factor below 1, drawdown above cap, or level discipline under 70% →
  quarantine.
- Borderline (thin positive edge, drawdown over 75% of cap) → a decision for
  the owner, never a silent change.
- Nothing leaves quarantine on its own. The lab tunes, re-backtests, and a
  human promotes with evidence.

## Tuning loop (each strategy progresses on its own)

Every base strategy declares a search space of knobs (direction, hold time,
target distance, which confirmations are required, session window, conviction
floor). A tuner run samples combinations it has never evaluated, backtests each
on every paper symbol with next-open fills, and scores it on three time
splits: train, validate, test. The results store remembers every
(combination, symbol) ever run, so the search keeps widening night after
night instead of circling the current recipe.

A challenger is proposed only when every pooled split is positive with enough
trades, the test profit factor clears a floor, and it beats the incumbent on
both validate and test by a margin. Its symbol allowlist is the set of symbols
where validate and test are both positive, so a variant is tracked per asset.

Proposals are journal rows (`variants`): the dashboard accepts them over the
authenticated API, refuses any without positive evidence on all three splits,
and the bot registers them in the shadow book on its next pass. One unpromoted
challenger per base; a newer one retires the older unless the owner already
promoted it. Promotion to paper or live goes through the same gate as every
strategy. The tuner never trades.

## Evolution harness (2026-11-06)

The tuning loop grew into a harness with four roles, split between the Mac
(data and CPU) and the bot (journal and books):

| role | where | what it does |
|---|---|---|
| Explorer | Mac, nightly | the tuner over every classic base: new combinations of existing recipes, long-only when the account cannot short |
| Inventor | Mac, nightly | the same tuner over the *composite* bases, whose grammar composes a trigger (order-block retest, fair-value-gap retest, wick rejection at a key level, level break) with confirmations (15m ribbon, 5m break of structure, 15m swing trend, volume), a stop rule and a target rule: new recipes, not re-weights |
| Auditor | bot, daily review and `/api/audit` | per paper/live strategy since the ladder reset: expectancy, profit factor, last-10, overhold share (reached the target, gave it back), stop share, drift from the time-split evidence it was promoted on; per day: peak realized gain vs close ("gave back" days). A strategy with no edge over enough trades becomes a pending decision for the owner; the auditor never demotes |
| Reporter | Mac, nightly | `research/evolve/<date>.md`: the night's challengers plus the bot's audit |

`scripts/evolve.py` runs Explorer, Inventor and Reporter and submits
challengers to the shadow book; a launchd agent runs it at 02:00. Nothing in
the harness trades or promotes. Promotion still goes through the typed gate.

Price structure (`apexpm/structure.py`) is read from completed OHLC bars on
both timeframes and is identical live and in the backtester: fractal swings,
break of structure, order blocks (last opposing candle before an impulse that
breaks structure; mitigated once price trades back in), fair value gaps, and
wick rejections at key levels (ATR levels, anchor, prior-day high/low, today's
open). The conviction gate counts five structure confirmations on top of the
indicator ones, so every strategy's signals now carry a multi-timeframe
structure read and the size multiplier can act on it.

## Journal durability

The journal lives on a persistent disk at the host. A paper account reset changes
the broker's balance, not the journal: the bot detects the jump, re-seeds the
kill-switch anchor, and keeps every historical row.

## Independent audit

An audit pass separate from the builder runs on a cadence: daily (light:
yesterday's trades, rejections, exceptions, limits) and weekly (deep: code
review for correctness and look-ahead, strategy logic, process gaps). Obvious
fixes are applied and logged; anything else becomes a decision. The bar is
"we should not be finding much"; a noisy audit is itself a finding.

## Strategy analytics (2026-11-07)

`apexpm/strategy_analytics.py` is read-only over the journal and feeds the dashboard's Analytics tab:
three tiers (real money = live book, practice = paper book, lab = shadow book), each strategy judged on
the trades of the book it sits in now, with hit-target attainment and its 10-trade trend, biggest win
and loss in dollars and percent of capital tied up, and a running P&L line. `/api/strategy/analytics`
(`?all=1` for all time instead of since the ladder reset) and `/api/strategy/trades?name=` (the drill-down:
every trade with plain-language fields, filterable by ticker, result, how it ended and account). The
dashboard's JSON encoder sends null for infinite or NaN numbers so a strategy with no losses cannot
blank the page.

## Learning loop roles (2026-11-07)

Owner rule: analytics and learning run on ALL trades, in every book (real, paper, lab), all time.
Windows (24h / 7d / 30d / 90d) are for reporting only; a ladder reset never hides history.

- **Postmortem** (`apexpm/postmortem.py`, private): every closed trade is replayed from the 5-minute bars
  around it: fill slippage against the signal price, dollars risked as a share of equity, MFE/MAE while
  held, what-if results for fixed holds (15/30/60/120/240 min, the close) with the original stop honoured,
  the best exit available after ours, whether the target or the stop came first afterwards, and one
  verdict (good exit / left money / held too long / stop too tight). Stored in `trade_reviews`.
- **Brain** (`apexpm/brain.py`, private): aggregates the replays per strategy into lessons and
  recommendations (hold, stop, exit, pricing), stores them in `brain`, turns strong ones (n >= 15,
  >= 0.10R per trade) into tuner hints, and files one pending decision per lesson for the owner.
  `/api/brain` serves lessons and hints; the nightly `scripts/evolve.py` backtests the hints first
  (`tuner.run(extra=...)`) and reports back through `POST /api/harness/report`.
- **Warden** (`apexpm/warden.py`): every 30 minutes in the loop, checks that every role did its work
  (bot alive, every strategy scanned today, replays done, brain current, auditor within 24h, daily
  review written, weekly recap written, nightly tuner reported within 36h), runs the repair it is
  allowed to run, and files one decision per failing duty per day. `/api/warden` shows the last
  patrol; the Overview card "Is every job getting done?" renders it.
- The daily review now carries the Brain and Warden sections after the Auditor.
- `trades.signal_entry` keeps the price the signal asked for (entry becomes the fill) so slippage is real.

## Data retention (2026-11-07)

The journal (one SQLite file on the Render disk) is the product: every trade, rejection, scan, report,
replay and lesson since launch, never pruned by a ladder reset. Protection:
- `Journal.snapshot()` (SQLite online backup under the shared lock) and `Journal.health()` (quick_check,
  counts, size).
- `GET /api/backup` (token-gated) streams a consistent snapshot; `GET /api/data` shows health and the
  last backup. The Settings tab has "Download the full journal now".
- `scripts/backup.py` runs nightly from `scripts/tune_nightly.sh` before the tuner: pulls the snapshot
  to `data/backups/latest.db` plus a gzipped dated copy (14 days kept, plus the last 8 Sundays),
  verifies integrity, rewrites ONE small `data/exports/` folder of CSVs (no scan/rejection logs), and reports to
  `POST /api/harness/report` as source "backup". Both folders are gitignored (private data).
- Warden duties "journal healthy" (every patrol) and "journal backed up" (within 36h, Tue-Sat).

## Earned size and the owner's lens (2026-11-07)

- `apexpm/earned.py`: per-strategy size tier on FILLED trades over the review window: proving x1.00,
  earning x1.25 (>= 10 wins, 50%, PF 1.2), proven x1.50 (>= 25 wins, 55%, +0.20R, PF 1.4), stacking
  x2.00 (>= 50 wins, 55%, +0.25R, PF 1.5). Steps down one tier while the last 10 are net negative or on a
  4-loss streak; never below base; never lifts the band, notional or buying-power caps. Applied in the
  loop after the confluence multiplier, journaled as `earned_tier` / `earned_mult` in the trade notes.
  `EARNED_SIZE_ENABLED=0` switches it off.
- `brain.patterns_for`: the worst and best quarter of a strategy's closed trades (by dollars), the
  traits over-represented in each (ticker, time of day, how it ended, market, confirmations, hold,
  replay verdict, fill cost), and the "easy fixes" those imply (drop a ticker, no entries in a window,
  widen the stop, take profit sooner, require 4 confirmations, use a limit). Fixes are filed once each as
  owner decisions; nothing changes a live setting.
