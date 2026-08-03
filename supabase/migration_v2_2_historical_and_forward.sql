-- Alpaca Dip-Reversal Trigger Lab v2.2.0
-- Adds distinct historical sealed backtests and true-forward sealed tests,
-- each staged from 30 to 90 total US trading sessions.

begin;

alter table public.dip_trigger_runs add column if not exists confirmation_target_sessions integer;
alter table public.dip_trigger_runs add column if not exists confirmation_max_sessions integer;
alter table public.dip_trigger_runs add column if not exists confirmation_stage text;
alter table public.dip_trigger_runs add column if not exists confirmation_anchor_date date;
alter table public.dip_trigger_runs add column if not exists backtest_scope text;

update public.dip_trigger_runs
set confirmation_target_sessions = coalesce(confirmation_target_sessions, forward_target_sessions),
    confirmation_max_sessions = coalesce(confirmation_max_sessions, forward_max_sessions),
    confirmation_stage = coalesce(confirmation_stage, forward_stage),
    confirmation_anchor_date = coalesce(
      confirmation_anchor_date,
      case when run_mode='forward_sealed' then start_date else null end
    )
where run_mode='forward_sealed';

do $$
begin
  if exists (select 1 from pg_constraint where conname='dip_trigger_runs_run_mode_check') then
    alter table public.dip_trigger_runs drop constraint dip_trigger_runs_run_mode_check;
  end if;
  alter table public.dip_trigger_runs
    add constraint dip_trigger_runs_run_mode_check
    check (run_mode in ('discovery','forward_sealed','historical_sealed'));

  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_confirmation_sessions_check') then
    alter table public.dip_trigger_runs
      add constraint dip_trigger_runs_confirmation_sessions_check
      check (
        run_mode='discovery'
        or (
          confirmation_target_sessions in (30,90)
          and confirmation_max_sessions=90
          and confirmation_anchor_date is not null
        )
      );
  end if;

  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_backtest_scope_check') then
    alter table public.dip_trigger_runs
      add constraint dip_trigger_runs_backtest_scope_check
      check (
        backtest_scope is null
        or backtest_scope in ('end_to_end','frozen_parent_universe')
      );
  end if;
end $$;

update public.dip_trigger_runtime
set version='2.2.0', updated_at=now()
where id=1;

commit;
