# Deployment — v2.2.1

## What this release changes

v2.2.1 fixes the version shown in the web header. The header now reads the runtime package version dynamically, so it cannot remain on an older release label after deployment.

The v2.2 historical sealed backtest and true-forward sealed test remain unchanged.

## 1. Run the Supabase migration

In the same Supabase project used by the trigger lab:

1. Open **SQL Editor → New query**.
2. Open `supabase/migration_v2_2_historical_and_forward.sql`.
3. Copy the complete file into Supabase.
4. Select **Run**.

The migration adds:

```text
confirmation_target_sessions
confirmation_max_sessions
confirmation_stage
confirmation_anchor_date
backtest_scope
```

It also permits the new `historical_sealed` run mode and backfills existing v2.1 forward runs.

It does not delete existing runs, candidates, trials or metrics.

### Verify

```sql
select column_name
from information_schema.columns
where table_schema='public'
  and table_name='dip_trigger_runs'
  and column_name in (
    'confirmation_target_sessions',
    'confirmation_max_sessions',
    'confirmation_stage',
    'confirmation_anchor_date',
    'backtest_scope'
  )
order by column_name;
```

Five rows should be returned.

## 2. Update GitHub

1. Extract `alpaca_dip_reversal_trigger_lab_v2.2.1.zip`.
2. Upload everything inside the extracted folder to the existing repository.
3. Replace existing files.
4. Ensure this new file exists:

```text
supabase/migration_v2_2_historical_and_forward.sql
```

5. Commit:

```text
Add sealed historical and forward 30-to-90 testing
```

## 3. Redeploy Render

Redeploy both services:

```text
alpaca-dip-timing-web
alpaca-dip-timing-worker
```

No environment-variable changes are required.

Expected build:

```text
44 passed
```

Expected health version:

```json
{
  "status": "ok",
  "version": "2.2.1",
  "trading_enabled": false
}
```

## 4. Create a proper historical sealed backtest

Open the completed discovery run containing the frozen winner.

Under **Historical confirmation**, choose:

### Preferred: end-to-end

Use when the database contains auditable historical scanner alerts for the selected period.

- Scope: `End-to-end: same candidate source`
- Last historical date: a completed date strictly before the discovery run starts.

The app selects the 30 US trading sessions ending on or before that date. Results remain sealed until all 30 have been processed.

### Trigger-only fallback: frozen parent universe

Use only when historical scanner alerts do not exist.

- Scope: `Trigger-only: frozen parent symbol universe`

The app freezes every distinct symbol from the parent candidate set, then tests the exact reversal rule across the earlier historical block. The result evaluates the trigger, not the upstream scanner’s ability to select those symbols historically.

### Extension

A 30-session pass unlocks:

```text
Extend unchanged rule to 90 sessions
```

The original 30 sessions remain included. The extension adds 60 earlier sessions, producing one contiguous 90-session historical block ending at the same anchor date.

A successful historical 90-session verdict is:

```text
backtest_90_pass_for_forward_testing
```

It supports a true-forward test—not paper trading by itself.

## 5. Create a true-forward sealed test

On the same discovery run, select **True forward confirmation**.

- First forward date must be strictly after the discovery run end.
- The date must already have occurred.
- The app waits until 30 completed US trading sessions exist.
- No partial results are exposed.

If fewer than 30 sessions exist, the verdict is:

```text
forward_window_incomplete
```

Select **Recheck completed-session availability** later.

A successful 30-session result unlocks an unchanged extension to 90 total sessions.

A successful final forward verdict is:

```text
forward_90_pass_for_paper_testing
```

## 6. Evidence interpretation

- Historical 30 pass: worth extending historically.
- Historical 90 pass: worth true-forward confirmation.
- Forward 30 pass: worth extending forward.
- Forward 90 pass: supports tightly controlled paper testing.
- Any fail: reject the frozen trigger.
- Inconclusive: insufficient independent signals; do not alter thresholds inside the run.


## Upgrading from v2.2.2 after a failed backtest

No Supabase migration is required. Deploy v2.2.3 to both services, open the failed historical or forward child run, and use Retry/Resume. The frozen configuration and parent link remain unchanged.
