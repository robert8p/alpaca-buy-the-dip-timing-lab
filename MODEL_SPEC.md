# Model and confirmation specification — v2.2.4

## Objective

Identify temporary intraday oversold dislocations where selling exhaustion and reversal confirmation leave enough remaining upside to exceed a 3% net target after realistic costs.

## Frozen trigger

A confirmation child evaluates exactly one recipe and one price segment selected before the child is created. The rule’s:

- candidate protocol;
- symbols or scanner populations;
- search window;
- oversold thresholds;
- volume threshold;
- target, stop and cost assumptions;
- recipe and price segment

are immutable and hashed.

## Historical sealed backtest

A historical child must:

- end strictly before the parent discovery run begins;
- have no overlapping sessions with the parent;
- use 30 completed US trading sessions initially;
- expose no interim outcomes;
- perform no trigger selection;
- retain the original 30 when extended to 90 total sessions.

### End-to-end scope

Uses the parent candidate source unchanged. Scanner-based tests require historical scanner alerts with auditable point-in-time availability.

### Frozen-parent-universe scope

Uses the distinct parent candidate symbols as an immutable universe. This isolates trigger performance but does not validate historical scanner selection.

## True-forward sealed test

A forward child must:

- begin strictly after the parent discovery end date;
- wait for genuinely later completed sessions;
- use the first 30 sessions from its anchor;
- reveal no partial results;
- retain those 30 when extended to the first 90 total sessions.

## Execution model

- Features use only completed bars available at trigger time.
- Entry is the next exact one-minute bar.
- A missing next-minute bar blocks the trade.
- Same-bar target and stop is treated as stop-first.
- Gap-through stops exit at the worse opening price.
- Selection and gates use the highest configured cost assumption.
- Coverage is checked using only minutes available at the trigger, with complete recent minute history. Later missing bars cannot remove an earlier signal from the candidate population.
- Missing SPY minutes cannot provide relative-strength confirmation.
- A missing minute before the recorded exit makes the outcome unresolved. Unresolved outcomes remain counted in candidate evidence and block promotion, rather than being silently discarded from a profitable subset.
- Frozen confirmation configurations include the execution evidence version. Historical results remain available but require a new discovery and confirmation run under current checks; old and new evidence cannot silently be combined.

## Thirty-session gate

All must pass:

- at least 5 signals;
- at least 5 dates;
- at least 5 symbols;
- at least 75% net-target-before-stop success;
- positive mean and median net return;
- profit factor at least 1.5;
- 5% loss rate no greater than 25%;
- no symbol or date contributes more than 40% of positive profit.

## Ninety-session gate

All must pass:

- at least 12 signals;
- at least 10 dates;
- at least 10 symbols;
- at least 70% target success;
- mean net return at least 0.5%;
- positive median return;
- profit factor at least 1.5;
- 5% loss rate no greater than 25%;
- no symbol or date contributes more than 30% of positive profit.

## Verdict hierarchy

Historical:

```text
backtest_30_pass_extension_available
backtest_30_fail
backtest_30_inconclusive
backtest_90_pass_for_forward_testing
backtest_90_fail
backtest_90_inconclusive
```

Forward:

```text
forward_30_pass_extension_available
forward_30_fail
forward_30_inconclusive
forward_90_pass_for_paper_testing
forward_90_fail
forward_90_inconclusive
```

Historical success is not forward success. Only a 90-session true-forward pass supports paper testing.
