-- Alpaca Dip-Reversal Trigger Lab v2.1.0
-- Adds staged 30/90-session true-forward sealed confirmation.

begin;

alter table public.dip_trigger_runs add column if not exists run_mode text not null default 'discovery';
alter table public.dip_trigger_runs add column if not exists parent_run_id uuid;
alter table public.dip_trigger_runs add column if not exists forward_target_sessions integer;
alter table public.dip_trigger_runs add column if not exists forward_max_sessions integer;
alter table public.dip_trigger_runs add column if not exists forward_stage text;
alter table public.dip_trigger_runs add column if not exists frozen_config jsonb;
alter table public.dip_trigger_runs add column if not exists frozen_config_sha256 text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_parent_run_fk') then
    alter table public.dip_trigger_runs
      add constraint dip_trigger_runs_parent_run_fk foreign key (parent_run_id)
      references public.dip_trigger_runs(id) on delete restrict;
  end if;
  if not exists (select 1 from pg_constraint where conname='dip_trigger_runs_run_mode_check') then
    alter table public.dip_trigger_runs
      add constraint dip_trigger_runs_run_mode_check check (run_mode in ('discovery','forward_sealed'));
  end if;
end $$;

update public.dip_trigger_runtime
set version='2.1.0', updated_at=now()
where id=1;

commit;
