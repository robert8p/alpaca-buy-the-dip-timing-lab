# Alpaca Dip-Reversal Trigger Discovery & Confirmation Lab v2.2.1

A research-only web and worker application for discovering and validating intraday oversold-reversal triggers using Alpaca one-minute bars and Supabase.

The app contains **no order endpoints**.

## Research workflow

1. **Discovery run**
   - Tests pre-registered oversold → exhaustion → reversal recipes.
   - Uses chronological discovery, validation and sealed splits.
   - Freezes one recipe, price segment and all thresholds when evidence is compelling.

2. **Historical sealed backtest**
   - Tests the frozen rule on an earlier, non-overlapping block.
   - Starts with 30 completed US trading sessions.
   - A pass unlocks an unchanged extension to 90 total trading sessions.
   - Historical evidence is never labelled forward evidence.

3. **True-forward sealed test**
   - Starts strictly after the parent discovery run.
   - Waits until 30 genuinely later trading sessions exist.
   - A pass unlocks an unchanged extension to 90 total sessions.
   - A 90-session pass supports controlled paper testing only.

## Historical backtest scopes

- **End-to-end**: preserves the original candidate source. For scanner-based runs this requires auditable historical scanner alerts. This is the preferred full-strategy backtest.
- **Frozen parent universe**: freezes the parent run’s distinct symbols and tests the reversal trigger across the historical block. This validates the trigger mechanics, not the upstream scanner’s historical selection.

## Sealing and integrity

Confirmation children store a SHA-256 hash covering:

- run mode;
- parent run;
- historical/forward anchor;
- candidate protocol;
- frozen recipe and segment;
- price, liquidity and oversold thresholds;
- search window, costs, target and stop.

Results, signal counts, metrics, issues and exports remain hidden while a 30- or 90-session sealed run is processing.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md). Existing v2.1 installations must run:

```text
supabase/migration_v2_2_historical_and_forward.sql
```

before deploying the new code.
