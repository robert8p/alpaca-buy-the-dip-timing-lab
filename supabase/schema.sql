-- Alpaca Dip-Reversal Trigger Discovery Lab v2.2.0
-- Additive schema. Safe to run in the existing scanner Supabase project.

begin;
create extension if not exists pgcrypto;

create table if not exists public.dip_trigger_runs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  run_mode text not null default 'discovery',
  parent_run_id uuid,
  forward_target_sessions integer,
  forward_max_sessions integer,
  forward_stage text,
  confirmation_target_sessions integer,
  confirmation_max_sessions integer,
  confirmation_stage text,
  confirmation_anchor_date date,
  backtest_scope text,
  frozen_config jsonb,
  frozen_config_sha256 text,
  source_mode text not null check (source_mode in ('scanner_alerts','manual_symbols','candidate_csv')),
  start_date date not null,
  end_date date not null,
  symbols jsonb not null default '[]'::jsonb,
  scanner_scan_types jsonb not null default '["pre_open","midday"]'::jsonb,
  search_start_et time not null default '09:45',
  search_end_et time not null default '15:15',
  trigger_recipes jsonb not null default '["deep_higher_low","deep_prior_high_break","capitulation_higher_low","relative_strength_turn","vwap_reclaim_after_deep"]'::jsonb,
  target_net_pct double precision not null default 3.0 check (target_net_pct > 0 and target_net_pct <= 50),
  target_gross_pct double precision not null default 3.5 check (target_gross_pct > 0 and target_gross_pct <= 50),
  stop_loss_pct double precision not null default 5.0 check (stop_loss_pct > 0 and stop_loss_pct <= 50),
  cost_bps jsonb not null default '[15,20,50]'::jsonb,
  min_price double precision not null default 2.0,
  max_price double precision not null default 50.0,
  min_dollar_volume double precision not null default 5000000,
  min_drawdown_high_pct double precision not null default 5.0,
  min_drawdown_open_pct double precision not null default 2.0,
  min_below_vwap_pct double precision not null default 1.0,
  oversold_memory_minutes integer not null default 15,
  volume_climax_ratio double precision not null default 2.5,
  min_history_bars integer not null default 30,
  discovery_ratio double precision not null default 0.60 check (discovery_ratio > 0 and discovery_ratio < 1),
  validation_ratio double precision not null default 0.20 check (validation_ratio > 0 and validation_ratio < 1),
  status text not null default 'queued' check (status in ('queued','running','completed','completed_with_warnings','failed','cancelled')),
  stage text not null default 'queued',
  candidate_count integer not null default 0,
  completed_candidate_count integer not null default 0,
  trial_count integer not null default 0,
  issue_count integer not null default 0,
  retry_count integer not null default 0,
  cancel_requested boolean not null default false,
  sealed_opened boolean not null default false,
  winner_recipe text,
  winner_segment text,
  verdict text,
  result_json jsonb,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  heartbeat_at timestamptz,
  completed_at timestamptz,
  last_error text,
  check (end_date >= start_date),
  check (search_end_et > search_start_et),
  check (discovery_ratio + validation_ratio < 1),
  check (run_mode='discovery' or (parent_run_id is not null and sealed_opened=true and forward_target_sessions in (30,90) and forward_max_sessions=90 and winner_recipe is not null and winner_segment is not null))
);


-- v2.2.0 additive historical and forward confirmation fields for existing installations.
alter table public.dip_trigger_runs add column if not exists run_mode text not null default 'discovery';
alter table public.dip_trigger_runs add column if not exists parent_run_id uuid;
alter table public.dip_trigger_runs add column if not exists forward_target_sessions integer;
alter table public.dip_trigger_runs add column if not exists forward_max_sessions integer;
alter table public.dip_trigger_runs add column if not exists forward_stage text;
alter table public.dip_trigger_runs add column if not exists confirmation_target_sessions integer;
alter table public.dip_trigger_runs add column if not exists confirmation_max_sessions integer;
alter table public.dip_trigger_runs add column if not exists confirmation_stage text;
alter table public.dip_trigger_runs add column if not exists confirmation_anchor_date date;
alter table public.dip_trigger_runs add column if not exists backtest_scope text;
alter table public.dip_trigger_runs add column if not exists frozen_config jsonb;
alter table public.dip_trigger_runs add column if not exists frozen_config_sha256 text;

update public.dip_trigger_runs
set confirmation_target_sessions=coalesce(confirmation_target_sessions,forward_target_sessions),
    confirmation_max_sessions=coalesce(confirmation_max_sessions,forward_max_sessions),
    confirmation_stage=coalesce(confirmation_stage,forward_stage),
    confirmation_anchor_date=coalesce(confirmation_anchor_date,case when run_mode='forward_sealed' then start_date else null end)
where run_mode='forward_sealed';

do $$
begin
  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_parent_run_fk') then
    alter table public.dip_trigger_runs
      add constraint dip_trigger_runs_parent_run_fk foreign key (parent_run_id)
      references public.dip_trigger_runs(id) on delete restrict;
  end if;
  if exists (select 1 from pg_constraint where conname='dip_trigger_runs_run_mode_check') then
    alter table public.dip_trigger_runs drop constraint dip_trigger_runs_run_mode_check;
  end if;
  alter table public.dip_trigger_runs
    add constraint dip_trigger_runs_run_mode_check check (run_mode in ('discovery','forward_sealed','historical_sealed'));
  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_confirmation_sessions_check') then
    alter table public.dip_trigger_runs add constraint dip_trigger_runs_confirmation_sessions_check
      check (run_mode='discovery' or (confirmation_target_sessions in (30,90) and confirmation_max_sessions=90 and confirmation_anchor_date is not null));
  end if;
  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_backtest_scope_check') then
    alter table public.dip_trigger_runs add constraint dip_trigger_runs_backtest_scope_check
      check (backtest_scope is null or backtest_scope in ('end_to_end','frozen_parent_universe'));
  end if;
end $$;

create index if not exists dip_trigger_runs_status_idx on public.dip_trigger_runs(status,created_at);

create table if not exists public.dip_trigger_candidates (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.dip_trigger_runs(id) on delete cascade,
  symbol text not null,
  trade_date date not null,
  source text not null,
  source_scan_types jsonb not null default '[]'::jsonb,
  source_details jsonb not null default '{}'::jsonb,
  available_at timestamptz not null,
  session_open_at timestamptz,
  session_close_at timestamptz,
  split text check (split in ('discovery','validation','sealed_test')),
  status text not null default 'queued' check (status in ('queued','running','completed','skipped','failed','sealed')),
  bar_count integer not null default 0,
  quality_flags jsonb not null default '[]'::jsonb,
  retry_count integer not null default 0,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  last_error text,
  unique (run_id,symbol,trade_date)
);
create index if not exists dip_trigger_candidates_status_idx on public.dip_trigger_candidates(run_id,status,trade_date,symbol);
create index if not exists dip_trigger_candidates_split_idx on public.dip_trigger_candidates(run_id,split,trade_date);

create table if not exists public.dip_trigger_trials (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_trigger_runs(id) on delete cascade,
  candidate_id uuid not null references public.dip_trigger_candidates(id) on delete cascade,
  symbol text not null,
  trade_date date not null,
  split text not null check (split in ('discovery','validation','sealed_test')),
  recipe_key text not null,
  trigger_at timestamptz not null,
  entry_at timestamptz not null,
  entry_price double precision not null,
  trigger_features jsonb not null,
  target_price double precision not null,
  stop_price double precision not null,
  target_hit boolean not null,
  first_target_hit_at timestamptz,
  stop_hit boolean not null,
  first_stop_hit_at timestamptz,
  exit_at timestamptz not null,
  exit_price double precision not null,
  exit_reason text not null,
  gross_return_pct double precision not null,
  max_gain_pct double precision not null,
  max_drawdown_pct double precision not null,
  same_bar_ambiguous boolean not null default false,
  created_at timestamptz not null default now(),
  unique (candidate_id,recipe_key)
);
create index if not exists dip_trigger_trials_metric_idx on public.dip_trigger_trials(run_id,split,recipe_key,trade_date);

create table if not exists public.dip_trigger_metrics (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_trigger_runs(id) on delete cascade,
  split text not null check (split in ('discovery','validation','sealed_test')),
  segment_key text not null default 'all',
  recipe_key text not null,
  cost_bps integer not null,
  observations integer not null,
  independent_dates integer,
  symbols integer,
  mean_net_return_pct double precision,
  median_net_return_pct double precision,
  win_rate_pct double precision,
  net_target_success_rate_pct double precision,
  net_target_wilson_low_pct double precision,
  loss_5pct_rate_pct double precision,
  positive_date_rate_pct double precision,
  positive_symbol_rate_pct double precision,
  worst_fold_mean_pct double precision,
  profit_factor double precision,
  bootstrap_ci_low_pct double precision,
  bootstrap_ci_high_pct double precision,
  p_value double precision,
  q_value double precision,
  metrics_json jsonb not null,
  created_at timestamptz not null default now(),
  unique (run_id,split,segment_key,recipe_key,cost_bps)
);
create index if not exists dip_trigger_metrics_idx on public.dip_trigger_metrics(run_id,split,cost_bps,segment_key,recipe_key);

create table if not exists public.dip_trigger_issues (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_trigger_runs(id) on delete cascade,
  candidate_id uuid references public.dip_trigger_candidates(id) on delete cascade,
  symbol text,
  trade_date date,
  stage text not null,
  severity text not null default 'warning' check (severity in ('info','warning','error')),
  message text not null,
  created_at timestamptz not null default now()
);
create index if not exists dip_trigger_issues_idx on public.dip_trigger_issues(run_id,created_at desc);

create table if not exists public.dip_trigger_runtime (
  id integer primary key default 1 check (id=1),
  version text not null default '2.2.0',
  worker_status text not null default 'not_started',
  active_run_id uuid,
  heartbeat_at timestamptz,
  last_error text,
  updated_at timestamptz not null default now()
);
insert into public.dip_trigger_runtime(id,version,worker_status)
values (1,'2.2.0','not_started')
on conflict (id) do update set version=excluded.version,updated_at=now();

alter table public.dip_trigger_runs enable row level security;
alter table public.dip_trigger_candidates enable row level security;
alter table public.dip_trigger_trials enable row level security;
alter table public.dip_trigger_metrics enable row level security;
alter table public.dip_trigger_issues enable row level security;
alter table public.dip_trigger_runtime enable row level security;

create or replace function public.claim_next_dip_trigger_run()
returns setof public.dip_trigger_runs
language plpgsql
security definer
set search_path=public
as $$
declare v_id uuid;
begin
  select id into v_id
  from public.dip_trigger_runs
  where status='queued' and cancel_requested=false
  order by created_at asc
  for update skip locked
  limit 1;
  if v_id is null then return; end if;
  return query
  update public.dip_trigger_runs
  set status='running',
      stage=case when candidate_count=0 then 'generating_candidates' else 'processing_candidates' end,
      started_at=coalesce(started_at,now()),heartbeat_at=now(),last_error=null
  where id=v_id and status='queued'
  returning *;
end;
$$;
revoke all on function public.claim_next_dip_trigger_run() from public,anon,authenticated;
grant execute on function public.claim_next_dip_trigger_run() to service_role;

commit;
