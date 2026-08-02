select version, worker_status, active_run_id, heartbeat_at, last_error
from public.dip_runtime where id = 1;

select id, name, source_mode, start_date, end_date, status, stage,
       candidate_count, completed_candidate_count, trial_count, issue_count,
       winner_segment, winner_variant, verdict, last_error, created_at, heartbeat_at, completed_at
from public.dip_runs
order by created_at desc
limit 20;

select status, count(*)
from public.dip_candidates
where run_id = 'PASTE_RUN_ID_HERE'::uuid
group by status
order by status;
