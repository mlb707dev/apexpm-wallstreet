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
