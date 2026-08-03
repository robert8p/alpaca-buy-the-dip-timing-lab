# Changelog

## 2.0.1

- Restored v1 scanner-schema compatibility assertions in the v2 package so GitHub uploads overwrite stale `tests/test_worker_sql.py` files.
- Retained and explicitly tested `first_alerted_at`, `live_scan_jobs.cutoff_at`, and calibration-job metadata handling.
- Added conditional recovery for stale v1 timing-lab runs when legacy `dip_*` tables coexist with v2.
- Kept v2 `dip_trigger_*` stale-work recovery fully qualified.

## 2.0.0

- Replaced fixed-time optimisation with state-based dip-reversal trigger discovery.
- Added nine pre-registered oversold/exhaustion/reversal recipes.
- Added SPY-relative-strength features.
- Added point-in-time oversold memory and volume-climax logic.
- Added exact next-minute entries and first-trigger-only sampling.
- Added date, symbol and chronological-fold consistency metrics.
- Added concentration controls for best stock and best date.
- Required discovery support before validation can select a recipe.
- Added exceptional small-sample classification for a frozen sealed test only.
- Added isolated `dip_trigger_*` tables so v1 timing results remain untouched.
- Retained no-trading architecture and conservative path simulation.
