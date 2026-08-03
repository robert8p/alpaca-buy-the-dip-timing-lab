select
  to_regclass('public.dip_trigger_runs') as runs,
  to_regclass('public.dip_trigger_candidates') as candidates,
  to_regclass('public.dip_trigger_trials') as trials,
  to_regclass('public.dip_trigger_metrics') as metrics,
  to_regclass('public.dip_trigger_issues') as issues,
  to_regclass('public.dip_trigger_runtime') as runtime;

select version,worker_status,active_run_id,heartbeat_at,last_error
from public.dip_trigger_runtime
where id=1;
