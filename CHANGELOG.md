# Changelog

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
