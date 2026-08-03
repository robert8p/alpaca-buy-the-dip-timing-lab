# Troubleshooting

## `relation public.dip_trigger_runtime does not exist`

Run `supabase/schema.sql` in the same Supabase project referenced by `DATABASE_URL`.

## Worker starts but no candidates are created

For scanner mode, confirm the database contains `live_signal_alerts`. Run:

```sql
select scan_type,min(trade_date),max(trade_date),count(*)
from public.live_signal_alerts
group by scan_type;
```

For manual mode, confirm symbols were entered.

## Many scanner alerts are excluded

The app excludes alerts whose auditable availability is after the configured search end. Historical calibration rows are recognised from scanner-job metadata. Exclusion is preferable to look-ahead bias.

## No trigger trials

This means candidates did not satisfy both the oversold state and a selected reversal recipe. It is not an application failure. Review `quality_flags` and `issues.csv` before changing any threshold.

## `relative_strength_turn` never fires

Check that SPY one-minute bars are available from the configured Alpaca feed. Other recipes continue to run if SPY is unavailable.

## Run appears stuck

Check:

```sql
select version,worker_status,active_run_id,heartbeat_at,last_error
from public.dip_trigger_runtime
where id=1;
```

Then:

```sql
select status,count(*)
from public.dip_trigger_candidates
where run_id='YOUR-RUN-ID'
group by status;
```

If the heartbeat is current, the worker is active. The interface refreshes every 15 seconds.

## A run failed after a Render restart

Deploy the latest commit and use **Retry and resume**. Stale running candidates and runs are requeued with explicit table aliases to avoid the previous PostgreSQL ambiguity bug.

## Negative result

Do not relax thresholds after viewing outcomes and rerun until something wins. That converts research into overfitting. A negative result is the intended stopping condition.

## Docker build fails in `tests/test_worker_sql.py`

Cause: v1 test files remained in GitHub because uploading v2.0.0 did not delete files omitted from the new package. Version 2.0.2 includes replacement compatibility tests and retains the corresponding scanner audit protections. Upload the complete v2.0.2 `tests` directory and redeploy. The expected build result is `32 passed`.


## Build passes but the web container exits before Uvicorn logs

Version 2.0.2 starts Uvicorn directly from the Dockerfile and no longer depends on the executable bit of `scripts/start_web.sh`. In Render, clear any stale Docker Command override or set it exactly to `python -m uvicorn app.main:app --host 0.0.0.0 --port 10000`, then redeploy. Also confirm `APP_PASSWORD` is at least 12 characters and `SESSION_SECRET` is at least 32 characters.
