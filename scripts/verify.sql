select
  to_regclass('public.dip_trigger_runs') as runs,
  to_regclass('public.dip_trigger_candidates') as candidates,
  to_regclass('public.dip_trigger_trials') as trials,
  to_regclass('public.dip_trigger_metrics') as metrics,
  to_regclass('public.dip_trigger_issues') as issues,
  to_regclass('public.dip_trigger_runtime') as runtime;

select column_name,data_type
from information_schema.columns
where table_schema='public'
  and table_name='dip_trigger_runs'
  and column_name in ('run_mode','parent_run_id','forward_target_sessions','forward_max_sessions','forward_stage','frozen_config','frozen_config_sha256')
order by column_name;

select version,worker_status,active_run_id,heartbeat_at,last_error
from public.dip_trigger_runtime
where id=1;
