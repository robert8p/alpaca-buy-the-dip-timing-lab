# Alpaca Dip-Reversal Trigger Discovery Lab v2.0.0

A research-only FastAPI application that identifies **state-based intraday dip-reversal triggers** rather than optimising a fixed clock time.

## Purpose

The app asks:

> When a stock becomes unusually oversold, which observable combination of selling exhaustion and reversal confirmation consistently leaves enough upside to earn at least the configured net target after costs?

It does not ask which minute is generally best. Entry time is recorded as a descriptive outcome of the trigger.

## Core workflow

1. Load candidates from the existing Alpaca scanner, a manual symbol list, or CSV.
2. Download split-adjusted one-minute stock bars and SPY benchmark bars.
3. Search minute by minute after the candidate becomes auditable.
4. Require a recent oversold state: drawdown from the session high, decline from the open, distance below VWAP, price and liquidity gates.
5. Test pre-registered reversal recipes such as higher lows, prior-bar-high breaks, capitulation-volume contraction, VWAP reclaim and relative-strength recovery.
6. Enter on the next exact one-minute bar open.
7. Simulate target-before-stop ordering conservatively, including same-bar ambiguity and gap-through stops.
8. Measure profitability after 15, 20 and 50 bps costs.
9. Reject triggers that depend on one date, one stock, one chronological fold or one unusually favourable period.
10. Keep the final chronological split sealed unless a validation trigger qualifies.

## Evidence labels

- `validation_passed_pending_sealed_test`: strict material-consistency gates passed.
- `promising_but_unproven_pending_sealed_test`: meaningful but below the strongest evidence threshold.
- `compelling_small_sample_pending_sealed_test`: exceptionally strong across every available validation date, but too small for validation. This can be frozen and tested on the existing sealed dates; it is not live-trading approval.
- `rejected_no_materially_consistent_trigger`: no recipe was compelling enough. The correct action is to stop, not enlarge the window merely to rescue it.

## Safety and research integrity

- No account, position or order endpoints.
- No live trading.
- No future information in trigger construction.
- Exact next-minute entry; missing bars are not forward-filled.
- Same-minute target and stop is scored as stop first.
- Gap-through stops use the worse opening price.
- Selection uses the highest configured cost assumption.
- Benjamini-Hochberg correction across trigger and price-segment tests.
- Discovery support is required before validation can select a trigger.

See `DEPLOYMENT.md` for deployment and `MODEL_SPEC.md` for the exact research design.
