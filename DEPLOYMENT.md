# Step-by-step deployment

This version replaces the web and worker code of the existing Buy-the-Dip Timing Lab but uses new `dip_trigger_*` tables. Existing `dip_*` timing-lab data remains untouched.

## Upgrade from the failed v2.0.0 build

The v2.0.0 ZIP did not contain `tests/test_worker_sql.py` or `tests/test_config.py`. GitHub web uploads replace matching files but do not delete files that are absent from the upload, so the v1 copies remained in the repository and were executed by Docker.

For v2.0.2, upload **all files**, including the complete `tests` folder. Confirm that GitHub shows these four test files before redeploying:

```text
tests/test_config.py
tests/test_package.py
tests/test_research.py
tests/test_worker_sql.py
```

No Supabase migration or environment-variable change is required for this patch.

## 1. Download and extract

Extract `alpaca_dip_reversal_trigger_lab_v2.0.2.zip`.

The files inside the extracted folder—not the outer folder itself—must sit at the root of the GitHub repository.

## 2. Install the additive Supabase schema

Use the same Supabase project as the Alpaca Live Signal Scanner.

1. Open Supabase.
2. Select the scanner project.
3. Open **SQL Editor → New query**.
4. Open `supabase/schema.sql` from this package.
5. Copy the entire file into the query.
6. Select **Run**.

Expected tables:

```text
dip_trigger_runs
dip_trigger_candidates
dip_trigger_trials
dip_trigger_metrics
dip_trigger_issues
dip_trigger_runtime
```

Verify:

```sql
select
  to_regclass('public.dip_trigger_runs') as runs,
  to_regclass('public.dip_trigger_candidates') as candidates,
  to_regclass('public.dip_trigger_trials') as trials,
  to_regclass('public.dip_trigger_metrics') as metrics,
  to_regclass('public.dip_trigger_runtime') as runtime;
```

Every column should show a table name, not `null`.

## 3. Replace the GitHub files

1. Open the existing timing-lab GitHub repository.
2. Upload everything inside the extracted v2.0.2 folder.
3. Replace existing files.
4. Commit:

```text
Refactor timing lab into dip-reversal trigger discovery
```

The Render service names remain:

```text
alpaca-dip-timing-web
alpaca-dip-timing-worker
```

This lets the existing Blueprint update those services rather than creating another pair.

## 4. Confirm Render environment variables

Web service:

```text
DATABASE_URL
APP_USERNAME
APP_PASSWORD
SESSION_SECRET
SESSION_COOKIE_SECURE=true
```

Worker:

```text
DATABASE_URL
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_FEED=sip
ALPACA_MARKET_DATA_RPM=60
BOOTSTRAP_ITERATIONS=2000
```

No values need to change from the working timing-lab deployment.

## 5. Redeploy

Render should redeploy automatically after the GitHub commit. Otherwise:

1. Open `alpaca-dip-timing-web`.
2. Select **Manual Deploy → Deploy latest commit**.
3. Repeat for `alpaca-dip-timing-worker`.

Expected worker log:

```text
Dip-reversal trigger worker started; feed=sip
```

Expected health response:

```text
https://YOUR-WEB-SERVICE.onrender.com/health
```

```json
{
  "status": "ok",
  "version": "2.0.2",
  "app": "alpaca-dip-reversal-trigger-lab",
  "trading_enabled": false
}
```

## 6. Run a three-symbol proof

Use:

```text
Candidate source: Manual symbol list
Symbols: AAPL, MSFT, NVDA
Start: 2026-07-20
End: 2026-07-20
Search: 09:45–15:15 ET
Recipes: deep_higher_low, deep_prior_high_break, vwap_reclaim_after_deep
```

Retain all default thresholds and costs.

Success means the run completes and either creates trigger trials or explicitly records that no trigger fired. A zero-trigger result is a valid research result.

## 7. Run the existing dataset without expanding it

Use the same auditable range already identified:

```text
Start: 2026-07-06
End: 2026-07-27
Candidate source: Existing scanner alerts
Scanner populations: Pre-open and Midday
Search: 09:45–15:15 ET
Recipes: all pre-registered recipes
Net target: 3.0%
Gross target: 3.5%
Stop: 5.0%
Costs: 15,20,50 bps
Price: US$2–US$50
Dollar volume: US$5,000,000
Drawdown from high: 5.0%
Decline from open: 2.0%
Below VWAP: 1.0%
Oversold memory: 15 minutes
Volume climax: 2.5×
```

Do not widen the dates merely because the verdict is negative.

## 8. Interpret the result

- `rejected_no_materially_consistent_trigger`: stop. Nothing in this dataset merits further work.
- `compelling_small_sample_pending_sealed_test`: freeze the recipe and open the existing sealed dates. Do not change thresholds.
- `promising_but_unproven_pending_sealed_test`: open the sealed test, but paper testing remains the maximum next step.
- `validation_passed_pending_sealed_test`: open the sealed test without modifying the rule.
- `validated_for_paper_testing`: paper test only; no live trading is enabled.

## 9. Export

Open the completed run and select **Download export**. Upload the ZIP back into ChatGPT for interpretation.


## v2.0.2 web startup

The Dockerfile launches Uvicorn directly. In the Render web service, the Docker Command field should normally be blank so Render uses the Dockerfile `CMD`. If an old override remains from an earlier version, either clear it or set it exactly to:

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port 10000
```
