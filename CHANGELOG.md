# Changelog

## 1.0.3 — Live-scanner schema compatibility fix

- Added support for the original scanner audit field `live_signal_alerts.first_alerted_at`.
- Joins `live_scan_jobs.cutoff_at` through `job_id` as the authoritative logical scan cutoff.
- Uses the latest actual audit timestamp for live alerts, preventing late-created rows from being simulated earlier.
- Uses logical job cutoff rather than backfill insertion time for rows explicitly marked `decision='calibration'`.
- Preserves the UK/US daylight-saving look-ahead guard.
- No Supabase schema migration is required.

## 1.0.2 — PostgreSQL stale-recovery fix

- Qualified `retry_count` and `last_error` with the `dip_candidates` target alias in the `UPDATE ... FROM` stale-work recovery query.
- Qualified all `dip_runs` columns in the companion recovery update for consistency and future safety.
- Prevents PostgreSQL `AmbiguousColumn` crashes during worker startup after a deploy or restart.
- Added an automated regression test for the recovery SQL.

## 1.0.1 — Render startup fix

- Removed the quoted web `dockerCommand` from `render.yaml`.
- Added `scripts/start_web.sh` to expand Render's `PORT` safely.
- Configured the Docker image to start the web service through its own `CMD`.
- Avoids Render interpreting the complete Uvicorn command as one executable name.

## 1.0.0 — 2026-08-02

- Separate FastAPI web service and resumable Render background worker.
- Additive Supabase `dip_*` schema; no live-scanner table modifications.
- Existing scanner-alert, manual-symbol and candidate-CSV sources.
- Point-in-time scanner-alert availability guard to prevent DST/look-ahead leakage.
- Alpaca market-calendar handling, including early closes.
- Exact fixed-minute entries with no missing-bar forward fill.
- Confirmation entries require an immediately consecutive next-minute bar.
- Conservative target-before-stop simulation and incomplete-close-path exclusion.
- Frozen all / US$2–5 / US$5–20 / US$20–50 price segments.
- Chronological discovery, validation and deferred sealed-test workflow.
- Sealed phase processes only the frozen validation segment and entry variant.
- Circular moving-block bootstrap confidence intervals and multiple-testing correction.
- Password-protected polished dashboard, progress, issues and hashed exports.
- Default Alpaca throttle reduced to 60 requests per minute to limit interference with other applications.
- Automated research-integrity tests plus package, route and Blueprint validation.
