select
  t.run_id,
  t.candidate_id,
  t.symbol,
  t.trade_date,
  t.split,
  t.variant_key,
  t.entry_at,
  t.entry_price,
  t.entry_reason,
  t.target_price,
  t.stop_price,
  t.target_hit,
  t.first_target_hit_at,
  t.stop_hit,
  t.first_stop_hit_at,
  t.exit_at,
  t.exit_price,
  t.exit_reason,
  t.gross_return_pct,
  t.max_gain_pct,
  t.max_drawdown_pct,
  t.same_bar_ambiguous,
  c.source_available_at,
  c.session_open_at,
  c.session_close_at,
  c.candidate_features,
  c.quality_flags
from public.dip_trials t
join public.dip_candidates c on c.id = t.candidate_id
where t.run_id = 'PASTE_RUN_ID_HERE'::uuid
order by t.trade_date, t.symbol, t.variant_key;
