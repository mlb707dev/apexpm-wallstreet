# Governance

## Strategy lifecycle

| Status | Meaning | Can order? |
|---|---|---|
| `research` | Evaluated and journaled every scan, never ordered | No |
| `paper` | Trades the paper account | Paper only |
| `live` | Trades real capital | Yes |
| `suspended` | Auto-paused by the daily review | No |
| `retired` | Removed by a human | No |

Promotion is manual and recorded. Demotion is automatic. The dashboard's
promote button records a free-text note as a governance event; the study
requirement below is policy, enforced by review, not by the button.

### Promotion bar

- research → paper: a backtest on a multi-year, out-of-sample window with
  positive expectancy after costs, acceptable drawdown, and a written
  rationale for why the edge exists.
- paper → live: 30 trading days or 100 closed paper trades, positive
  expectancy, drawdown inside the kill-switch threshold, zero unhandled
  exceptions, daily review run every session, explicit owner sign-off.

### Auto-suspension

The daily review computes rolling statistics per strategy over the last
`REVIEW_WINDOW_TRADES` closed trades. With at least `REVIEW_MIN_TRADES`, a
strategy is suspended when expectancy, profit factor, drawdown or level
discipline crosses the configured threshold. Re-enabling requires a human.

## Independent checks

Every material change gets a code review by a reviewer that did not write it.
Every backtest result gets a review for look-ahead bias, fill optimism, and
sample size before it is used as evidence. Both reviews are logged.

## Change control

- Secrets live only in `.env`. Never in code, logs, the journal, or any site
  the agent is driven to.
- Live mode is never a default in any file.
- A parameter changes only with a study that justifies it.
