# Troubleshooting

## Web service fails with `Missing or insecure web settings`

Set these on the Render web service:

```text
DATABASE_URL
APP_PASSWORD        at least 12 characters
SESSION_SECRET      at least 32 characters
SESSION_COOKIE_SECURE=true
```

The Blueprint generates `SESSION_SECRET` automatically. Do not replace it with a short value.

## Login succeeds but returns to the login page

Render serves the site through HTTPS and the Blueprint uses a secure session cookie. Confirm you are opening the `https://` URL, not an `http://` URL.

For local HTTP development only, set:

```text
SESSION_COOKIE_SECURE=false
```

Do not use that setting on Render.

## Worker fails with missing Alpaca settings

Set on the worker:

```text
DATABASE_URL
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_FEED=sip
```

Regenerating or resetting Alpaca credentials invalidates the old key pair.

## `relation public.dip_runs does not exist`

The Supabase schema was not run, or it was run in a different project. Open the project used by `DATABASE_URL`, run `supabase/schema.sql`, then restart both Render services.

## `live_signal_alerts was not found`

You selected **Existing midday scanner alerts**, but the connected Supabase project does not contain the live scanner tables.

Recovery:

- point `DATABASE_URL` at the existing scanner project;
- use Manual symbols; or
- upload a candidate CSV.

## Scanner-alert mode says no auditable timestamp exists

The app requires `cutoff_at`, `decision_at` or `created_at` on `live_signal_alerts`. It will not assume a candidate was known before 12:30 ET.

Use Manual symbols or a candidate CSV. Do not remove the guard: it prevents look-ahead bias, including UK–US daylight-saving mismatch weeks.

## Scanner candidates were excluded as available after cutoff

This is expected when an alert was generated or persisted after the research cutoff. The excluded row appears in `dip_issues`.

Do not move its timestamp backwards. Use a later pre-registered entry window in a separate run if that is the real decision process you want to test.

## `password authentication failed`

The `DATABASE_URL` contains the wrong database password, or special characters were not URL-encoded.

Copy the Session pooler string again from Supabase **Connect**, replace the password carefully, and update both Render services.

## Connection errors to a `db....supabase.co` host

The direct database endpoint may require IPv6. Use the **Session pooler** URL on port `5432`.

## `prepared statement already exists` or transaction-pooler errors

The app disables psycopg prepared statements, but the recommended connection remains the Session pooler on port `5432`. Replace a port-6543 transaction-pooler URL if errors persist.

## Alpaca says recent SIP data is not permitted

Use completed historical sessions whose query end is outside the delayed-data restriction, or use a subscription that permits the requested recent SIP period.

For connectivity testing only, `ALPACA_FEED=iex` can be used in a separate run. Do not pool IEX and SIP results as though they were the same dataset.

## The research app may affect the live scanner's Alpaca limit

Alpaca request budgets can be shared. The supplied worker is capped at 60 requests per minute.

Safest options:

1. run the historical calibration outside live scan windows;
2. use a separate available credential/account budget;
3. lower `ALPACA_MARKET_DATA_RPM` further.

Do not raise the limit until the proof run succeeds and you understand the aggregate account usage.

## Run produces zero trials

Open the run and inspect candidate status counts and issues.

Common reasons:

- no imported midday alerts in the selected date range;
- every candidate failed the dip gate;
- uploaded dates were weekends or holidays;
- no regular-session bars were returned;
- the exact requested entry minute was absent;
- a confirmation occurred but the immediately consecutive entry minute was absent;
- no valid close was available for a target-or-close outcome;
- the candidate was outside the validation-selected price segment during sealed processing.

For a pipeline proof, use 2–3 liquid symbols over a completed week and uncheck **Apply the frozen dip gate**.

## Why did 12:31 not become 12:32?

That is intentional. A missing 12:31 bar may reflect a halt or no executable print. Forward-filling it would test a different entry while still labelling it 12:31.

Create a separate fixed 12:32 variant before the run if you explicitly want to test 12:32.

## Candidate says `insufficient_point_in_time_bar_coverage`

With the dip gate active, the exact session-open bar and exact 12:29 bar must exist for a 12:30 signal. The candidate is excluded rather than calculating the signal from stale data.

## Run appears stuck

Check:

1. dashboard stage and heartbeat;
2. Render worker logs;
3. Supabase query:

```sql
select id, status, stage, candidate_count, completed_candidate_count,
       trial_count, issue_count, heartbeat_at, last_error
from public.dip_runs
order by created_at desc
limit 10;
```

A recent heartbeat with rising completed candidates means the run is active. If the worker redeploys, stale work is automatically requeued after the configured 15-minute safety window.

## A candidate failed

Use **Retry and resume** on the same run. Completed trials are retained. The app blocks sealed opening while discovery/validation failures remain.

## Duplicate trials

The database has a unique constraint on `(candidate_id, variant_key)`. Retrying a run cannot duplicate a completed trial.

## Why are candidates marked `sealed`?

That is the untouched final 20% of dates. They have not been processed. Review and freeze the validation winner before selecting **Open sealed test**.

## Sealed test was opened but no results appear yet

Opening the sealed test queues a second worker phase. Wait for the run to move through `queued`, `running` and `completed_sealed_test`.

Only the validation-selected entry variant and price segment are processed.

## Sealed test was opened too early

Do not retune the rule and continue calling that split untouched. Create a new run with genuinely unused future dates or a new historical holdout period.

## Pre-sealed export does not contain the final dates

That is intentional. Before the sealed test is opened, the web export excludes sealed candidates, trials and metrics.

## Render worker is unavailable on a free plan

Render background workers require a paid instance type. The Blueprint requests Starter. The web service can be resized later, but the worker must exist while runs are processing.


## Run-size guard triggered

Manual runs are limited to 100 predeclared symbols and 200,000 symbol × calendar-day combinations. Candidate CSVs are limited to 100,000 rows. These guards prevent accidental multi-million-row jobs and oversized in-memory exports. Reduce the universe or date range without changing the hypothesis after viewing results.
