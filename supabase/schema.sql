-- Alpaca Buy-the-Dip Timing Lab v1.0.2
-- Additive schema. Safe to run in the existing live-scanner Supabase project.

begin;

create extension if not exists pgcrypto;

create table if not exists public.dip_runs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  source_mode text not null check (source_mode in ('scanner_alerts','manual_symbols','candidate_csv')),
  start_date date not null,
  end_date date not null,
  symbols jsonb not null default '[]'::jsonb,
  cutoff_et time not null default '12:30',
  fixed_entry_times_et jsonb not null default '["12:31","12:35","12:45","13:00"]'::jsonb,
  confirmation_variants jsonb not null default '["higher_low","vwap_reclaim","prior_bar_high","five_bar_break"]'::jsonb,
  target_net_pct double precision not null default 3.0 check (target_net_pct > 0 and target_net_pct <= 50),
  target_gross_pct double precision not null default 3.5 check (target_gross_pct > 0 and target_gross_pct <= 50),
  stop_loss_pct double precision not null default 5.0 check (stop_loss_pct > 0 and stop_loss_pct <= 50),
  cost_bps jsonb not null default '[15,20,50]'::jsonb,
  apply_dip_filter boolean not null default true,
  min_price double precision not null default 2.0,
  max_price double precision not null default 50.0,
  min_dollar_volume double precision not null default 5000000,
  dip_from_high_pct double precision not null default 3.0,
  dip_from_open_pct double precision not null default 1.0,
  require_below_vwap boolean not null default true,
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
  winner_variant text,
  winner_segment text,
  verdict text,
  result_json jsonb,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  heartbeat_at timestamptz,
  completed_at timestamptz,
  last_error text,
  check (end_date >= start_date),
  check (discovery_ratio + validation_ratio < 1)
);

create index if not exists dip_runs_status_idx on public.dip_runs(status, created_at);
create index if not exists dip_runs_created_idx on public.dip_runs(created_at desc);

create table if not exists public.dip_candidates (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.dip_runs(id) on delete cascade,
  symbol text not null,
  trade_date date not null,
  cutoff_at timestamptz not null,
  source text not null,
  source_alert_id text,
  source_available_at timestamptz,
  session_open_at timestamptz,
  session_close_at timestamptz,
  split text check (split in ('discovery','validation','sealed_test')),
  candidate_features jsonb,
  passed_dip_filter boolean,
  filter_reasons jsonb not null default '[]'::jsonb,
  status text not null default 'queued' check (status in ('queued','running','completed','skipped','failed','sealed')),
  bar_count integer not null default 0,
  quality_flags jsonb not null default '[]'::jsonb,
  retry_count integer not null default 0,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  last_error text,
  unique (run_id, symbol, trade_date)
);

alter table public.dip_candidates
  add column if not exists source_available_at timestamptz,
  add column if not exists session_open_at timestamptz,
  add column if not exists session_close_at timestamptz;

create index if not exists dip_candidates_run_status_idx on public.dip_candidates(run_id, status, trade_date, symbol);
create index if not exists dip_candidates_split_idx on public.dip_candidates(run_id, split, trade_date);

create table if not exists public.dip_trials (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_runs(id) on delete cascade,
  candidate_id uuid not null references public.dip_candidates(id) on delete cascade,
  symbol text not null,
  trade_date date not null,
  split text not null check (split in ('discovery','validation','sealed_test')),
  variant_key text not null,
  entry_at timestamptz not null,
  entry_price double precision not null,
  entry_reason text not null,
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
  unique (candidate_id, variant_key)
);

create index if not exists dip_trials_run_variant_idx on public.dip_trials(run_id, split, variant_key, trade_date);

create table if not exists public.dip_metrics (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_runs(id) on delete cascade,
  split text not null check (split in ('discovery','validation','sealed_test')),
  segment_key text not null default 'all',
  variant_key text not null,
  cost_bps integer not null,
  observations integer not null,
  independent_dates integer,
  symbols integer,
  mean_net_return_pct double precision,
  median_net_return_pct double precision,
  win_rate_pct double precision,
  net_target_success_rate_pct double precision,
  net_target_wilson_low_pct double precision,
  net_target_daily_ci_low_pct double precision,
  loss_5pct_rate_pct double precision,
  target_before_stop_rate_pct double precision,
  stop_before_target_rate_pct double precision,
  bootstrap_ci_low_pct double precision,
  bootstrap_ci_high_pct double precision,
  p_value double precision,
  q_value double precision,
  metrics_json jsonb not null,
  created_at timestamptz not null default now(),
  unique (run_id, split, segment_key, variant_key, cost_bps)
);

alter table public.dip_runs
  add column if not exists target_net_pct double precision not null default 3.0,
  add column if not exists winner_segment text;
alter table public.dip_metrics
  add column if not exists segment_key text not null default 'all',
  add column if not exists net_target_success_rate_pct double precision,
  add column if not exists net_target_wilson_low_pct double precision,
  add column if not exists net_target_daily_ci_low_pct double precision,
  add column if not exists loss_5pct_rate_pct double precision;

create index if not exists dip_metrics_run_idx on public.dip_metrics(run_id, split, segment_key, cost_bps, variant_key);

create table if not exists public.dip_issues (
  id bigint generated always as identity primary key,
  run_id uuid not null references public.dip_runs(id) on delete cascade,
  candidate_id uuid references public.dip_candidates(id) on delete cascade,
  symbol text,
  trade_date date,
  stage text not null,
  severity text not null default 'warning' check (severity in ('info','warning','error')),
  message text not null,
  created_at timestamptz not null default now()
);

create index if not exists dip_issues_run_idx on public.dip_issues(run_id, created_at desc);

create table if not exists public.dip_runtime (
  id integer primary key default 1 check (id = 1),
  version text not null default '1.0.2',
  worker_status text not null default 'not_started',
  active_run_id uuid,
  heartbeat_at timestamptz,
  last_error text,
  updated_at timestamptz not null default now()
);

insert into public.dip_runtime(id, version, worker_status)
values (1, '1.0.2', 'not_started')
on conflict (id) do update set version = excluded.version, updated_at = now();

alter table public.dip_runs enable row level security;
alter table public.dip_candidates enable row level security;
alter table public.dip_trials enable row level security;
alter table public.dip_metrics enable row level security;
alter table public.dip_issues enable row level security;
alter table public.dip_runtime enable row level security;

create or replace function public.claim_next_dip_run()
returns setof public.dip_runs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
begin
  select id into v_id
  from public.dip_runs
  where status = 'queued' and cancel_requested = false
  order by created_at asc
  for update skip locked
  limit 1;

  if v_id is null then
    return;
  end if;

  return query
  update public.dip_runs
     set status = 'running',
         stage = case when candidate_count = 0 then 'generating_candidates' else 'processing_candidates' end,
         started_at = coalesce(started_at, now()),
         heartbeat_at = now(),
         last_error = null
   where id = v_id and status = 'queued'
   returning *;
end;
$$;

revoke all on function public.claim_next_dip_run() from public, anon, authenticated;
grant execute on function public.claim_next_dip_run() to service_role;

commit;
