# Step-by-step deployment

Deploy this as a **separate private GitHub repository and separate Render Blueprint**, while normally pointing it at the **same Supabase project as the existing stock scanner**.

The app does not replace the live scanner. Its SQL is additive and creates only `dip_*` objects.

## Before starting

You need:

- this package ZIP;
- access to the Supabase project used by the stock scanner;
- the Supabase database password;
- Alpaca paper-market-data credentials;
- GitHub and Render accounts.

Because Alpaca request limits can be shared across applications, the Blueprint defaults to only **60 requests per minute**. Run the first full historical calibration outside the live scanner's critical scan window. A separate Alpaca credential/account budget is preferable where available.

## 1. Extract and check the package

1. Download the supplied ZIP.
2. In Windows File Explorer, right-click it and select **Extract All**.
3. Open the extracted folder.
4. Confirm these appear directly inside it:

```text
app
supabase
tests
render.yaml
Dockerfile
requirements.txt
DEPLOYMENT.md
```

Success means there is no second nested folder between the extracted folder and `render.yaml`.

Do not upload the unopened ZIP itself as the GitHub repository contents.

The package also includes `examples/candidate_dates_template.csv` for candidate-date uploads.

## 2. Suspend nothing

Leave the existing live scanner deployed. This app uses different Render service names and different database table names.

For the first full timing run, queue it after the live scanner has completed its daily work, or on a weekend. The proof run uses only a few historical requests.

## 3. Add the Supabase schema

1. Sign in to Supabase.
2. Open the project used by `alpaca-live-signal-scanner`.
3. Select **SQL Editor** in the left menu.
4. Select **New query**.
5. On your computer, open `supabase/schema.sql` in Notepad.
6. Press `Ctrl+A`, then `Ctrl+C`.
7. Paste the whole file into Supabase SQL Editor.
8. Select **Run**.

Expected result:

```text
Success. No rows returned
```

Then select **Table Editor** and confirm these tables exist:

```text
dip_runs
dip_candidates
dip_trials
dip_metrics
dip_issues
dip_runtime
```

The script does not delete or change `live_signal_alerts` or the scanner's existing outcomes.

### Common errors

- `permission denied`: ensure you are running the query as the project owner in Supabase SQL Editor.
- `already exists`: the script is designed to be additive; rerun the complete script rather than deleting tables.
- Query fails halfway: fix the displayed error and rerun the full file. `IF NOT EXISTS` and `ON CONFLICT` make this safe.

## 4. Copy the Supabase Session pooler URL

1. In the Supabase project, select **Connect** near the top.
2. Choose **Session pooler**.
3. Copy the PostgreSQL connection string using port `5432`.
4. Replace the password placeholder with the actual database password.
5. Add `?sslmode=require` if it is not already present.
6. Save it in a password manager as `DATABASE_URL`.

It should resemble:

```text
postgresql://postgres.PROJECT_REF:YOUR_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

Use the Session pooler, not the direct `db....supabase.co` hostname.

### Password contains special characters

URL-encode them. Common examples:

```text
@  becomes %40
#  becomes %23
%  becomes %25
/  becomes %2F
```

### Common errors

- `password authentication failed`: recopy or reset the Supabase database password and update the URL.
- `network unreachable` or IPv6 error: you copied the direct database host; return to **Connect → Session pooler**.
- `prepared statement already exists`: confirm port `5432`. The app also disables prepared statements for pooler compatibility.

## 5. Prepare Alpaca historical-data credentials

1. Sign in to Alpaca.
2. Open **Paper Trading**.
3. Open **API Keys**.
4. Select **Generate New Keys** if needed.
5. Copy both values immediately:

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
```

The app calls only market-data and market-calendar endpoints. It contains no order endpoint.

For isolation, use credentials that are not required by the live scanner where practical. If the same request budget must be shared, retain the supplied `ALPACA_MARKET_DATA_RPM=60` and run historical jobs outside the scanner's critical window.

## 6. Create the private GitHub repository

1. Sign in to GitHub.
2. Select **New repository**.
3. Repository name:

```text
alpaca-buy-the-dip-timing-lab
```

4. Select **Private**.
5. Leave README, `.gitignore` and licence creation unchecked.
6. Select **Create repository**.
7. On the empty repository page, select **uploading an existing file**.
8. Open the extracted package folder in File Explorer.
9. Select everything **inside** that folder.
10. Drag the selected files and folders onto GitHub.
11. Wait for every upload to finish.
12. Commit message:

```text
Initial buy-the-dip timing lab
```

13. Select **Commit changes**.

Success means the repository root directly shows:

```text
app
supabase
tests
render.yaml
Dockerfile
```

Never upload `.env`, database passwords or API keys.

## 7. Deploy the Render Blueprint

1. Sign in to Render.
2. Select **New → Blueprint**.
3. Connect GitHub if requested.
4. Select the private `alpaca-buy-the-dip-timing-lab` repository.
5. Render reads the root-level `render.yaml`.
6. Enter a Blueprint name such as:

```text
alpaca-dip-timing-lab
```

7. Confirm Render proposes these two services:

```text
alpaca-dip-timing-web
alpaca-dip-timing-worker
```

8. Enter the prompted secret values.

### Web service

```text
DATABASE_URL   Supabase Session pooler URL
APP_PASSWORD   unique password, at least 12 characters
```

The Blueprint sets `APP_USERNAME=admin`, generates `SESSION_SECRET`, and enforces secure cookies.

### Worker

```text
DATABASE_URL       same Supabase Session pooler URL
ALPACA_API_KEY     Alpaca paper key ID
ALPACA_SECRET_KEY  Alpaca paper secret
```

Keep these Blueprint defaults initially:

```text
ALPACA_FEED=sip
ALPACA_MARKET_DATA_RPM=60
BOOTSTRAP_ITERATIONS=2000
```

9. Select **Deploy Blueprint**.
10. Open each service's **Logs** page.
11. Wait until both services show **Live**.

The Docker build runs the automated test suite. A build that reports a failed test should not be forced through by editing the Dockerfile.

Render background workers require a paid service type. The Blueprint requests Starter for both services.

## 8. Verify the deployment

1. Open the public URL of `alpaca-dip-timing-web`.
2. Add `/health` to the end.

Example:

```text
https://YOUR-WEB-SERVICE.onrender.com/health
```

Expected shape:

```json
{
  "status": "ok",
  "version": "1.0.0",
  "app": "alpaca-buy-the-dip-timing-lab",
  "trading_enabled": false,
  "worker": {
    "worker_status": "idle"
  }
}
```

The worker heartbeat should become recent.

### If health returns 500

Open the web-service logs.

- `relation public.dip_runtime does not exist`: run `supabase/schema.sql` in the project used by `DATABASE_URL`.
- authentication or connection error: correct `DATABASE_URL` on both services.
- insecure setting error: `APP_PASSWORD` must be at least 12 characters; do not remove the generated `SESSION_SECRET`.

### If worker remains `not_started`

Open the worker logs and verify its three secret variables. A healthy startup includes:

```text
Buy-the-dip worker started; feed=sip
```

## 9. Sign in

1. Remove `/health` and open the main web URL.
2. Username:

```text
admin
```

3. Password: the `APP_PASSWORD` entered in Render.

The dashboard should show the worker as `idle`.

## 10. Run a small proof test

Use a completed historical week.

Enter:

```text
Run name: Pipeline proof
Candidate source: Manual symbol list
Symbols: AAPL, MSFT, NVDA
Start date: 2026-07-27
End date: 2026-07-31
Signal cutoff: 12:30
Fixed entries: 12:31,12:35
Net target: 3.0
Gross target trigger: 3.5
Stop: 5.0
Costs: 20
```

For this proof only:

- uncheck **Apply the frozen dip gate**;
- keep **Higher low** and **VWAP reclaim** checked.

Select **Queue timing calibration** once.

Successful behaviour:

- status moves from `queued` to `running`;
- real candidate rows appear;
- completed candidates and trial counts increase;
- Discovery and Validation metrics appear;
- the run ends at `awaiting_sealed_test` or `completed_no_validation_winner`.

An economically negative result is not a pipeline failure. The proof succeeds when real bars, entries and metrics were generated without fabricated rows.

## 11. Verify Supabase rows

Open **Supabase → SQL Editor → New query** and run:

```sql
select version, worker_status, active_run_id, heartbeat_at, last_error
from public.dip_runtime
where id = 1;

select id, name, status, stage, candidate_count,
       completed_candidate_count, trial_count, issue_count,
       winner_variant, verdict, last_error
from public.dip_runs
order by created_at desc
limit 10;
```

Success means:

- the heartbeat is recent;
- the proof run has candidate and trial rows;
- no unrecovered error is shown.

For candidate status:

```sql
select status, count(*)
from public.dip_candidates
where run_id = 'PASTE_RUN_ID'::uuid
group by status
order by status;
```

Before the sealed test is opened, the final chronological group should have status `sealed`.

## 12. Check whether the scanner history is large enough

Before the primary run, open **Supabase → SQL Editor → New query** and run:

```sql
select
  min(trade_date) as first_midday_date,
  max(trade_date) as last_midday_date,
  count(distinct trade_date) as independent_midday_dates,
  count(*) as alert_rows
from public.live_signal_alerts
where scan_type = 'midday';
```

The strict evidence gate needs at least 20 validation dates and 10 sealed dates. With a 60/20/20 chronological split, aim for **at least 100 independent candidate dates**, preferably more. A 60-calendar-day history usually cannot satisfy that gate and should be treated as an exploratory calibration only.

If the scanner history is shorter, either:

- use all available scanner history and accept that the result may remain `promising_but_unproven`; or
- run **Manual symbol list** over at least 12 months using a symbol universe fixed before viewing results.

## 13. Run the primary scanner-alert analysis

Create a new run using the full available scanner-alert period:

```text
Run name: Full-history midday-alert dip timing
Candidate source: Existing midday scanner alerts
Start date: first_midday_date from the query
End date: last completed date from the query
Cutoff: 12:30 ET
Fixed entries: 12:31,12:35,12:45,13:00
Confirmations: higher low, VWAP reclaim, prior-bar high
Costs: 15,20,50
Net target: 3.0%
Gross target trigger: 3.5%
Stop: 5.0%
Apply frozen dip gate: checked
Require below VWAP: checked
```

Keep the default price, liquidity and dip thresholds for the first substantive run. Queue it once.

The worker excludes any live scanner alert that was not actually available by 12:30 ET. It uses the latest live audit timestamp, so a row created late cannot inherit an earlier scheduled cutoff. Historical rows explicitly marked `calibration` instead use their logical historical cutoff; if needed, the app reconstructs 17:00 Europe/London with the correct daylight-saving conversion. Duplicate symbol/date alerts are collapsed to the earliest valid candidate.

If scanner-alert mode reports no compatible availability timestamp, use a candidate CSV or manual symbols rather than disabling the guard.

## 14. Monitor and recover

Healthy processing shows:

- recent worker and run heartbeats;
- an increasing completed-candidate count;
- stage text such as `processing_candidates_40_of_180`;
- increasing trials.

If Render redeploys, the worker automatically requeues stale work. Do not create a replacement run.

If a run ends `failed`, `cancelled` or `completed_with_warnings`:

1. Open the same run.
2. Read the latest issue and Render worker log.
3. Correct the environment or data-access problem.
4. Select **Retry and resume**.

Completed trials are protected by unique constraints and are not duplicated.

## 15. Freeze the validation decision

When Discovery and Validation finish, do not open the sealed test immediately.

Record:

- validation-selected price segment and entry variant;
- highest-cost probability of achieving +3% net and its date-resampled lower bound;
- highest-cost validation mean and median net return;
- observations and independent dates;
- confidence interval;
- q-value;
- pre-sealed verdict and warnings.

Do not alter entry times, filters, target, stop or cost assumptions after seeing validation and then reuse this run's sealed dates.

If any discovery/validation candidate remains failed, use **Retry and resume**. The app blocks sealed opening while failed candidates remain.

## 16. Open and process the sealed test

Only after accepting the frozen validation winner:

1. Open the run.
2. Select **Open sealed test**.
3. Confirm the warning.
4. The run returns to `queued`, then `running`.
5. The worker processes only the validation-selected variant on the untouched dates.
6. Wait for stage `completed_sealed_test`.

The final verdict cannot be upgraded by a good sealed result if validation did not meet the required gate.

`validated_for_paper_testing` means paper testing only. It does not authorise live capital.

## 17. Download the research package

Select **Download current export**.

Before the sealed test is opened, the ZIP deliberately excludes sealed candidates and outcomes. After sealed processing, it includes the sealed result for the frozen winner.

The ZIP contains:

```text
candidates.csv
trials.csv
metrics.csv
issues.csv
run.json
manifest.json
README.txt
```

`manifest.json` records every file's byte size and SHA-256 hash.

Upload the completed ZIP here for deeper robustness analysis without opening or editing individual CSV files first.
