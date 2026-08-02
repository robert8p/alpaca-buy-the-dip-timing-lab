# Alpaca Buy-the-Dip Timing Lab v1.0.3

A deployable research application that tests **when an intraday stock dip becomes executable**.

It is deliberately separate from the live scanner. It can connect to the same Supabase database and reuse existing `live_signal_alerts`, but it creates only new `dip_*` tables and contains no order-submission code.

## What it tests

For every stock/date candidate, the worker downloads split-adjusted one-minute Alpaca bars and compares:

### Fixed entry times

- 12:31 ET
- 12:35 ET
- 12:45 ET
- 13:00 ET

A fixed-time trial exists only when the exact requested one-minute bar exists. It never shifts a missing 12:31 bar to 12:32 or to a post-halt print.

### Confirmation entries

- first higher low followed by a green completed one-minute bar;
- first completed close reclaiming cumulative VWAP;
- first completed close above the previous bar's high;
- optional first completed close above the previous five bars' highs.

Every confirmation is followed by entry at the **immediately consecutive one-minute bar open**. A missing next minute invalidates that confirmation rather than silently delaying entry.

## Candidate sources

1. **Existing midday scanner alerts** from `public.live_signal_alerts`.
2. **Manual symbol list** over a selected date range.
3. **Candidate CSV** containing `trade_date,symbol`.

Scanner-alert mode audits when each source alert became available. An alert created after the 12:30 ET research cutoff is excluded. This prevents the spring/autumn UK–US daylight-saving mismatch from introducing look-ahead bias.

Manual-symbol and scanner-alert runs can apply the frozen baseline dip gate:

- price between US$2 and US$50;
- at least US$5 million notional volume by 12:30 ET;
- at least 3% below the morning high;
- at least 1% below the regular-session open;
- below cumulative VWAP.

These defaults are hypotheses, not validated findings. They are editable before a run and frozen thereafter.

The analysis also pre-registers four cutoff-price segments: all candidates, US$2–5, US$5–20 and US$20–50. The frozen winner therefore identifies both an entry method and, where supported, the price segment in which it works. Multiple-testing correction covers the complete entry-by-segment comparison.

## Outcome logic

Default economic assumptions:

- net success target: +3.0% after the tested cost assumption;
- gross target trigger: +3.5%;
- stop: -5.0%;
- cost stress: 15, 20 and 50 basis points round trip.

The path is replayed minute by minute from the entry bar. If a minute bar contains both the target and stop, the stop is assumed to occur first. If neither is reached, a market-close exit is accepted only when the expected final session bar exists. Alpaca's market calendar is used so early-close sessions are handled correctly.

## Research controls

- Chronological 60% discovery / 20% validation / 20% sealed test by trading date.
- Winner selected from validation, not discovery.
- Sealed candidates are **not processed** until the user explicitly opens the sealed test.
- Once opened, only the validation-selected variant is run on sealed dates.
- Pre-sealed exports exclude sealed candidates and outcomes.
- Event-level Wilson and date-resampled confidence bounds for the probability of achieving the net target.
- Circular moving-block bootstrap confidence intervals for mean net return.
- Benjamini-Hochberg correction across entry variants.
- Minimum sample, profitability and concentration gates before `validated_for_paper_testing`.
- Restart-safe candidate and trial persistence.
- Split-adjusted bars, official market sessions and America/New_York time-zone handling.
- Audited live-alert availability plus correct logical handling of historical calibration rows and UK–US daylight-saving mismatch weeks.
- ZIP exports with SHA-256 file hashes.

## Evidence-size warning

The strict 60/20/20 workflow needs enough independent candidate dates. Because the validation gate requires 20 dates and the sealed gate requires 10, a 60-calendar-day run is normally diagnostic only. Use all available scanner history or at least 12 months of a predeclared manual universe for a genuinely conclusive attempt.

## Verdicts

- `validation_passed_pending_sealed_test`: validation passed the strict gates, but no paper testing is authorised until the frozen sealed test also passes.
- `validated_for_paper_testing`: the untouched sealed result retained robust positive performance after a strict validation pass. This authorises paper testing only.
- `promising_but_unproven_pending_sealed_test`: validation is directionally positive but below the strict gate; the frozen sealed test may confirm only a promising classification.
- `promising_but_unproven`: final evidence is positive but insufficient for paper testing.
- `rejected`: no reliable positive expectancy after costs.
- `sealed_test_unavailable`: the selected validation entry was not executable in the sealed split.
- `rejected_no_trials` / `rejected_no_validation_evidence`: data or sample did not support a valid test.

## Repository structure

```text
app/
  alpaca.py       Alpaca historical-bars and market-calendar client
  auth.py         Private dashboard authentication
  config.py       Environment validation
  db.py           Pooled PostgreSQL access
  exports.py      Reproducible ZIP exports
  main.py         FastAPI dashboard and controls
  research.py     Dip gate, entries, path simulation and statistics
  worker.py       Resumable two-stage background worker
  templates/      Dashboard HTML
  static/         Dashboard styling
supabase/
  schema.sql
  migration_add_dip_lab.sql
tests/
scripts/
render.yaml
Dockerfile
DEPLOYMENT.md
MODEL_SPEC.md
TROUBLESHOOTING.md
```

## Security

- Keep the Supabase connection string and Alpaca keys only in Render environment variables.
- Use a private GitHub repository.
- Do not upload `.env`.
- The dashboard uses a password, a signed session cookie and secure-cookie mode on Render.
- The application has no Alpaca trading endpoint or account/order logic.

See `DEPLOYMENT.md` for browser-only deployment steps.
