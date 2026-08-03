# Troubleshooting — v2.2.2

## `column ... confirmation_target_sessions does not exist`

Run `supabase/migration_v2_2_historical_and_forward.sql` in the same Supabase project referenced by Render’s `DATABASE_URL`, then redeploy.

## Historical run returns no candidates

For `end_to_end` scanner mode, the selected period must contain auditable rows in `live_signal_alerts`. Check:

```sql
select min(trade_date), max(trade_date), count(distinct trade_date), count(*)
from public.live_signal_alerts
where scan_type in ('pre_open','midday');
```

If the historical period predates scanner calibration, use `frozen_parent_universe` only when the intention is to test the trigger itself. Do not describe that result as an end-to-end scanner backtest.

## `backtest_window_insufficient`

Alpaca returned fewer than the required 30 or 90 sessions ending at the historical anchor. Confirm the anchor date and data coverage. The app will not fabricate or overlap sessions.

## `forward_window_incomplete`

The required later sessions do not exist yet. This is expected for true-forward testing. Use **Recheck completed-session availability** after more US trading sessions have completed.

## Frozen integrity failure

A stored rule, anchor, scope or parent reference differs from the immutable hash. Do not edit confirmation rows manually. Create a new child from the original discovery run.

## Export returns HTTP 409

Confirmation exports remain deliberately locked while the 30- or 90-session window is queued or running.

## Historical 90 extension adds older dates

This is intentional. The initial historical test is the 30 sessions immediately preceding the selected end date. Extending to 90 preserves those 30 and adds the preceding 60 sessions.

## Forward 90 extension

The forward extension keeps the first 30 sessions and adds the following 60 sessions, producing the first 90 sessions after the forward anchor.

## Internal Server Error when creating a backtest or forward test

Upgrade to v2.2.2 or later. v2.2.1 could pass a native PostgreSQL UUID into the frozen JSON configuration, causing an unhandled serialization error. No database migration or rerun of the parent discovery analysis is required.

After deployment, return to the completed parent run and create the child again. If an earlier failed click created no child row, the button remains available.
