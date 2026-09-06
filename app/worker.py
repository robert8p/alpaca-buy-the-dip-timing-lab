from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from psycopg.types.json import Jsonb

from . import __version__
from .alpaca import AlpacaClient
from .config import settings
from .db import connection, execute, execute_many, fetch_all, fetch_one
from .research import (
    NY,
    CONFIRMATION_INITIAL_SESSIONS,
    CONFIRMATION_MAX_SESSIONS,
    FORWARD_INITIAL_SESSIONS,
    FORWARD_MAX_SESSIONS,
    SEGMENTS,
    TRIGGER_RECIPES,
    EVIDENCE_VERSION,
    IncompleteTrialEvidence,
    assign_chronological_splits,
    bars_to_frame,
    benjamini_hochberg,
    find_trigger_event,
    frozen_config_payload,
    frozen_config_sha256,
    verify_frozen_evidence_contract,
    job_marks_historical_calibration,
    latest_candidate_availability,
    materially_consistent,
    compelling_small_sample,
    evaluate_confirmation_gate,
    evaluate_forward_gate,
    performance_metrics,
    scheduled_scanner_cutoff,
    segment_matches,
    select_validation_record,
    simulate_trial,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("dip-trigger-worker")


def _issue(run_id: str, stage: str, message: str, severity: str = "warning", candidate_id: str | None = None, symbol: str | None = None, trade_date: date | None = None) -> None:
    execute(
        """
        insert into public.dip_trigger_issues(run_id,candidate_id,symbol,trade_date,stage,severity,message)
        values (%s,%s,%s,%s,%s,%s,%s)
        """,
        (run_id, candidate_id, symbol, trade_date, stage, severity, message[:4000]),
    )


def _update_runtime(status: str, run_id: str | None = None, error: str | None = None) -> None:
    execute(
        """
        update public.dip_trigger_runtime
        set version=%s, worker_status=%s, active_run_id=%s, heartbeat_at=now(), last_error=%s, updated_at=now()
        where id=1
        """,
        (__version__, status, run_id, error),
    )


def _heartbeat(run_id: str, stage: str | None = None) -> None:
    if stage:
        execute("update public.dip_trigger_runs set heartbeat_at=now(), stage=%s where id=%s", (stage, run_id))
    else:
        execute("update public.dip_trigger_runs set heartbeat_at=now() where id=%s", (run_id,))
    _update_runtime("running", run_id)


def _recover_stale_work() -> None:
    execute(
        """
        update public.dip_trigger_candidates as c
        set status='queued', retry_count=c.retry_count+1,
            last_error=coalesce(c.last_error || '; ','') || 'requeued_after_stale_worker'
        from public.dip_trigger_runs as r
        where c.run_id=r.id and c.status='running' and r.status='running'
          and coalesce(r.heartbeat_at,r.started_at,r.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )
    execute(
        """
        update public.dip_trigger_runs as r
        set status=case when r.cancel_requested then 'cancelled' else 'queued' end,
            stage=case when r.cancel_requested then 'cancelled_after_restart' else 'recovered_after_restart' end,
            retry_count=r.retry_count+1,
            last_error=case when r.cancel_requested then r.last_error else coalesce(r.last_error || '; ','') || 'worker heartbeat stale; requeued' end,
            completed_at=case when r.cancel_requested then now() else r.completed_at end
        where r.status='running'
          and coalesce(r.heartbeat_at,r.started_at,r.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )
    _recover_legacy_v1_stale_work()


def _recover_legacy_v1_stale_work() -> None:
    """Recover stale v1 timing-lab work when the legacy tables coexist.

    This is deliberately conditional: v2 uses isolated dip_trigger_* tables,
    but the same Supabase project may still contain unfinished v1 runs.
    """
    tables = fetch_one(
        """
        select to_regclass('public.dip_candidates') as candidates_table,
               to_regclass('public.dip_runs') as runs_table
        """
    )
    if not tables or not tables.get("candidates_table") or not tables.get("runs_table"):
        return
    execute(
        """
        update public.dip_candidates as c
        set status='queued', retry_count=c.retry_count+1,
            last_error=coalesce(c.last_error || '; ', '') || 'requeued_after_stale_worker'
        from public.dip_runs as dr
        where c.run_id=dr.id and c.status='running' and dr.status='running'
          and coalesce(dr.heartbeat_at,dr.started_at,dr.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )
    execute(
        """
        update public.dip_runs as dr
        set status=case when dr.cancel_requested then 'cancelled' else 'queued' end,
            stage=case when dr.cancel_requested then 'cancelled_after_restart' else 'recovered_after_restart' end,
            retry_count=dr.retry_count+1,
            last_error=case when dr.cancel_requested then dr.last_error else coalesce(dr.last_error || '; ', '') || 'worker heartbeat stale; requeued by v2 compatibility recovery' end,
            completed_at=case when dr.cancel_requested then now() else dr.completed_at end
        where dr.status='running'
          and coalesce(dr.heartbeat_at,dr.started_at,dr.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )


def _cancel_requested(run_id: str) -> bool:
    row = fetch_one("select cancel_requested from public.dip_trigger_runs where id=%s", (run_id,))
    return bool(row and row["cancel_requested"])


def _calendar_sessions(calendar: list[dict[str, Any]]) -> dict[date, tuple[datetime, datetime]]:
    sessions: dict[date, tuple[datetime, datetime]] = {}
    for row in calendar:
        trade_date = date.fromisoformat(str(row["date"]))
        open_clock = datetime.strptime(str(row.get("open") or "09:30")[:5], "%H:%M").time()
        close_clock = datetime.strptime(str(row.get("close") or "16:00")[:5], "%H:%M").time()
        sessions[trade_date] = (
            datetime.combine(trade_date, open_clock, tzinfo=NY),
            datetime.combine(trade_date, close_clock, tzinfo=NY),
        )
    return sessions


def _insert_candidates(run: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into public.dip_trigger_candidates(
                  run_id,symbol,trade_date,source,source_scan_types,source_details,available_at
                ) values (%s,%s,%s,%s,%s,%s,%s)
                on conflict (run_id,symbol,trade_date) do update set
                  available_at=least(public.dip_trigger_candidates.available_at,excluded.available_at),
                  source_scan_types=excluded.source_scan_types,
                  source_details=excluded.source_details
                """,
                [
                    (
                        run["id"], row["symbol"], row["trade_date"], row["source"],
                        Jsonb(row.get("source_scan_types") or []), Jsonb(row.get("source_details") or {}), row["available_at"],
                    )
                    for row in rows
                ],
            )
        conn.commit()
    return len(rows)


def _scanner_alert_candidates(run: dict[str, Any]) -> int:
    exists = fetch_one("select to_regclass('public.live_signal_alerts') as table_name")
    if not exists or not exists["table_name"]:
        raise RuntimeError("public.live_signal_alerts was not found; choose manual symbols or deploy into the scanner Supabase project")
    columns = {row["column_name"] for row in fetch_all("select column_name from information_schema.columns where table_schema='public' and table_name='live_signal_alerts'")}
    required = {"id", "trade_date", "symbol", "scan_type"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError("live_signal_alerts missing: " + ", ".join(missing))

    alert_availability = [name for name in ("cutoff_at", "decision_at", "created_at", "first_alerted_at") if name in columns]
    job_join = ""
    job_cutoff = False
    job_source_projection = "null::text as job_source"
    job_parameters_projection = "'{}'::jsonb as job_parameters"
    if "job_id" in columns:
        job_exists = fetch_one("select to_regclass('public.live_scan_jobs') as table_name")
        if job_exists and job_exists["table_name"]:
            job_columns = {row["column_name"] for row in fetch_all("select column_name from information_schema.columns where table_schema='public' and table_name='live_scan_jobs'")}
            if {"id", "cutoff_at"}.issubset(job_columns):
                job_join = "left join public.live_scan_jobs as j on j.id = a.job_id"
                job_cutoff = True
                if "source" in job_columns:
                    job_source_projection = "j.source::text as job_source"
                if "parameters" in job_columns:
                    job_parameters_projection = "j.parameters as job_parameters"
    availability_projection = [f"a.{name} as availability_{name}" for name in alert_availability]
    if job_cutoff:
        availability_projection.append("j.cutoff_at as availability_job_cutoff_at")
    if not availability_projection:
        raise RuntimeError("scanner alerts lack an auditable availability timestamp")

    decision_projection = "a.decision::text as source_decision" if "decision" in columns else "null::text as source_decision"
    decision_filter = "and a.decision is distinct from 'reject'" if "decision" in columns else ""
    security_filter = "and a.security_eligible is distinct from false" if "security_eligible" in columns else ""
    scan_types = [str(x) for x in (run.get("scanner_scan_types") or ["pre_open", "midday"])]
    alerts = fetch_all(
        f"""
        select a.id::text as source_alert_id,a.trade_date,upper(a.symbol) as symbol,a.scan_type,
               {decision_projection},{job_source_projection},{job_parameters_projection},
               {', '.join(availability_projection)}
        from public.live_signal_alerts as a
        {job_join}
        where a.trade_date between %s and %s
          and a.scan_type = any(%s)
          {decision_filter}
          {security_filter}
        order by a.trade_date,a.symbol,a.scan_type
        """,
        (run["start_date"], run["end_date"], scan_types),
    )
    merged: dict[tuple[str, date], dict[str, Any]] = {}
    excluded = 0
    for row in alerts:
        is_calibration = job_marks_historical_calibration(row)
        all_keys = list(alert_availability) + (["job_cutoff_at"] if job_cutoff else [])
        if is_calibration:
            logical = [key for key in all_keys if key not in {"created_at", "first_alerted_at"}]
            available_at = latest_candidate_availability([row.get(f"availability_{key}") for key in logical], row["trade_date"])
            if available_at is None:
                available_at = scheduled_scanner_cutoff(row["trade_date"], str(row["scan_type"]))
        else:
            available_at = latest_candidate_availability([row.get(f"availability_{key}") for key in all_keys], row["trade_date"])
        if available_at is None:
            excluded += 1
            _issue(str(run["id"]), "candidate_availability", "Excluded alert without auditable availability", symbol=row["symbol"], trade_date=row["trade_date"])
            continue
        search_end = datetime.combine(row["trade_date"], run["search_end_et"], tzinfo=NY)
        if available_at > search_end:
            excluded += 1
            _issue(str(run["id"]), "candidate_availability", f"Excluded alert available after search end: {available_at}", symbol=row["symbol"], trade_date=row["trade_date"])
            continue
        key = (row["symbol"], row["trade_date"])
        current = merged.get(key)
        details = {
            "alert_ids": sorted(set((current or {}).get("source_details", {}).get("alert_ids", []) + [row["source_alert_id"]])),
            "historical_calibration": bool(is_calibration or (current or {}).get("source_details", {}).get("historical_calibration")),
        }
        scan_list = sorted(set((current or {}).get("source_scan_types", []) + [str(row["scan_type"])]))
        if current is None or available_at < current["available_at"]:
            merged[key] = {
                "symbol": row["symbol"], "trade_date": row["trade_date"], "source": "live_signal_alerts",
                "source_scan_types": scan_list, "source_details": details, "available_at": available_at,
            }
        else:
            current["source_scan_types"] = scan_list
            current["source_details"] = details
    rows = sorted(merged.values(), key=lambda x: (x["trade_date"], x["symbol"]))
    _insert_candidates(run, rows)
    logger.info("Retained %s unique scanner candidates; excluded %s", len(rows), excluded)
    return len(rows)


def _manual_symbol_candidates(run: dict[str, Any], sessions: dict[date, tuple[datetime, datetime]]) -> int:
    symbols = sorted({str(x).strip().upper() for x in (run.get("symbols") or []) if str(x).strip()})
    if not symbols:
        raise RuntimeError("No manual symbols supplied")
    rows = []
    for trade_date in sorted(sessions):
        available_at = datetime.combine(trade_date, run["search_start_et"], tzinfo=NY)
        for symbol in symbols:
            rows.append({
                "symbol": symbol, "trade_date": trade_date, "source": "manual_symbols",
                "source_scan_types": [], "source_details": {}, "available_at": available_at,
            })
    return _insert_candidates(run, rows)


def _sync_calendar(run: dict[str, Any], sessions: dict[date, tuple[datetime, datetime]]) -> None:
    execute_many(
        """
        update public.dip_trigger_candidates
        set session_open_at=%s,session_close_at=%s
        where run_id=%s and trade_date=%s
        """,
        [(session_open, session_close, run["id"], trade_date) for trade_date, (session_open, session_close) in sessions.items()],
    )
    execute(
        """
        update public.dip_trigger_candidates
        set status='skipped',last_error='no_market_session',quality_flags='["no_market_session"]'::jsonb,completed_at=now()
        where run_id=%s and session_open_at is null and status='queued'
        """,
        (run["id"],),
    )


def _assign_splits(run: dict[str, Any]) -> None:
    if run.get("run_mode") in {"forward_sealed", "historical_sealed"}:
        execute(
            "update public.dip_trigger_candidates set split='sealed_test' where run_id=%s",
            (run["id"],),
        )
        return
    rows = fetch_all("select distinct trade_date from public.dip_trigger_candidates where run_id=%s order by trade_date", (run["id"],))
    mapping = assign_chronological_splits([row["trade_date"] for row in rows], float(run["discovery_ratio"]), float(run["validation_ratio"]))
    execute_many("update public.dip_trigger_candidates set split=%s where run_id=%s and trade_date=%s", [(split, run["id"], trade_date) for trade_date, split in mapping.items()])
    if not run.get("sealed_opened"):
        execute("update public.dip_trigger_candidates set status='sealed' where run_id=%s and split='sealed_test' and status='queued'", (run["id"],))


def _trigger_config(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "min_price": run["min_price"],
        "max_price": run["max_price"],
        "min_dollar_volume": run["min_dollar_volume"],
        "min_drawdown_high_pct": run["min_drawdown_high_pct"],
        "min_drawdown_open_pct": run["min_drawdown_open_pct"],
        "min_below_vwap_pct": run["min_below_vwap_pct"],
        "oversold_memory_minutes": run["oversold_memory_minutes"],
        "volume_climax_ratio": run["volume_climax_ratio"],
        "min_history_bars": run["min_history_bars"],
    }


def _insert_trials(run: dict[str, Any], candidate: dict[str, Any], results: list[Any]) -> None:
    if not results:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into public.dip_trigger_trials(
                  run_id,candidate_id,symbol,trade_date,split,recipe_key,trigger_at,entry_at,entry_price,
                  trigger_features,target_price,stop_price,target_hit,first_target_hit_at,stop_hit,first_stop_hit_at,
                  exit_at,exit_price,exit_reason,gross_return_pct,max_gain_pct,max_drawdown_pct,same_bar_ambiguous
                ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (candidate_id,recipe_key) do nothing
                """,
                [
                    (
                        run["id"], candidate["id"], candidate["symbol"], candidate["trade_date"], candidate["split"],
                        result.recipe_key, result.trigger_at, result.entry_at, result.entry_price, Jsonb(result.trigger_features),
                        result.target_price, result.stop_price, result.target_hit, result.first_target_hit_at,
                        result.stop_hit, result.first_stop_hit_at, result.exit_at, result.exit_price, result.exit_reason,
                        result.gross_return_pct, result.max_gain_pct, result.max_drawdown_pct, result.same_bar_ambiguous,
                    )
                    for result in results
                ],
            )
        conn.commit()


async def _benchmark_frame(trade_date: date, session_open: datetime, session_close: datetime, alpaca: AlpacaClient, cache: dict[date, pd.DataFrame]) -> pd.DataFrame:
    if trade_date in cache:
        return cache[trade_date]
    try:
        raw = await alpaca.bars("SPY", session_open, session_close + timedelta(minutes=1))
        cache[trade_date] = bars_to_frame(raw, session_open, session_close)
    except Exception as exc:
        logger.warning("SPY benchmark unavailable for %s: %s", trade_date, exc)
        cache[trade_date] = pd.DataFrame()
    return cache[trade_date]


async def _process_candidate(run: dict[str, Any], candidate: dict[str, Any], alpaca: AlpacaClient, benchmark_cache: dict[date, pd.DataFrame]) -> None:
    candidate_id = str(candidate["id"])
    execute("update public.dip_trigger_candidates set status='running',started_at=coalesce(started_at,now()),last_error=null where id=%s", (candidate_id,))
    trade_date = candidate["trade_date"]
    session_open = candidate.get("session_open_at")
    session_close = candidate.get("session_close_at")
    if session_open is None or session_close is None:
        execute("update public.dip_trigger_candidates set status='skipped',last_error='missing_market_session',completed_at=now() where id=%s", (candidate_id,))
        return
    quality: list[str] = []
    try:
        raw = await alpaca.bars(candidate["symbol"], session_open, session_close + timedelta(minutes=1))
        frame = bars_to_frame(raw, session_open, session_close)
        if frame.empty:
            execute("update public.dip_trigger_candidates set status='skipped',bar_count=0,last_error='no_regular_session_bars',quality_flags=%s,completed_at=now() where id=%s", (Jsonb(['no_regular_session_bars']), candidate_id))
            return
        if frame.iloc[0]["timestamp"] != pd.Timestamp(session_open):
            quality.append("missing_exact_session_open_bar")
            execute("update public.dip_trigger_candidates set status='skipped',bar_count=%s,last_error='missing_exact_session_open_bar',quality_flags=%s,completed_at=now() where id=%s", (len(frame), Jsonb(quality), candidate_id))
            return
        if len(frame) < int(run["min_history_bars"]) + 2:
            quality.append("insufficient_history_bars")
            execute("update public.dip_trigger_candidates set status='skipped',bar_count=%s,last_error='insufficient_history_bars',quality_flags=%s,completed_at=now() where id=%s", (len(frame), Jsonb(quality), candidate_id))
            return
        # Coverage is checked at each trigger in find_trigger_event. Filtering on
        # the later search window would select earlier signals using future data.
        benchmark = await _benchmark_frame(trade_date, session_open, session_close, alpaca, benchmark_cache)
        if benchmark.empty:
            quality.append("spy_benchmark_unavailable")

        configured_start = datetime.combine(trade_date, run["search_start_et"], tzinfo=NY)
        configured_end = datetime.combine(trade_date, run["search_end_et"], tzinfo=NY)
        available_at = candidate.get("available_at") or configured_start
        search_start = max(configured_start, available_at.astimezone(NY), session_open)
        search_end = min(configured_end, session_close - timedelta(minutes=2))
        if search_start >= search_end:
            execute("update public.dip_trigger_candidates set status='skipped',bar_count=%s,last_error='empty_search_window',quality_flags=%s,completed_at=now() where id=%s", (len(frame), Jsonb(quality + ["empty_search_window"]), candidate_id))
            return

        winner_only = str(run.get("winner_recipe") or "") if candidate["split"] == "sealed_test" else ""
        recipes = [winner_only] if winner_only else [str(x) for x in run["trigger_recipes"]]
        results = []
        for recipe in recipes:
            if recipe not in TRIGGER_RECIPES:
                quality.append(f"unknown_recipe_{recipe}")
                continue
            if recipe == "relative_strength_turn" and benchmark.empty:
                quality.append("relative_strength_recipe_skipped_no_spy")
                continue
            event = find_trigger_event(frame, benchmark, recipe, search_start, search_end, _trigger_config(run))
            if event is None:
                quality.append(f"trigger_not_reached_{recipe}")
                continue
            if candidate["split"] == "sealed_test" and not segment_matches(event.entry_price, str(run.get("winner_segment") or "all")):
                quality.append(f"outside_frozen_segment_{run.get('winner_segment') or 'all'}")
                continue
            try:
                result = simulate_trial(frame, event, float(run["target_gross_pct"]), float(run["stop_loss_pct"]), session_close)
            except IncompleteTrialEvidence as exc:
                # Retain the missing-outcome count in the candidate audit. A
                # later target must not replace an unknown intervening path.
                quality.append(f"unresolved_outcome_{recipe}")
                _issue(str(run["id"]), "outcome_evidence", str(exc), "warning", candidate_id, candidate["symbol"], trade_date)
                continue
            results.append(result)
        _insert_trials(run, candidate, results)
        execute(
            """
            update public.dip_trigger_candidates
            set status=%s,bar_count=%s,quality_flags=%s,completed_at=now(),last_error=%s
            where id=%s
            """,
            ("completed" if results else "skipped", len(frame), Jsonb(sorted(set(quality))), None if results else "no_trigger_recipe_reached", candidate_id),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        execute("update public.dip_trigger_candidates set status='failed',retry_count=retry_count+1,last_error=%s,completed_at=now() where id=%s", (message[:4000], candidate_id))
        _issue(str(run["id"]), "process_candidate", message, "error", candidate_id, candidate["symbol"], trade_date)


def _refresh_counts(run_id: str) -> None:
    row = fetch_one(
        """
        select count(*) as candidates,
               count(*) filter (where status in ('completed','skipped','failed','sealed')) as completed
        from public.dip_trigger_candidates where run_id=%s
        """,
        (run_id,),
    ) or {"candidates": 0, "completed": 0}
    trials = fetch_one("select count(*) as n from public.dip_trigger_trials where run_id=%s", (run_id,)) or {"n": 0}
    issues = fetch_one("select count(*) as n from public.dip_trigger_issues where run_id=%s", (run_id,)) or {"n": 0}
    execute(
        """
        update public.dip_trigger_runs set candidate_count=%s,completed_candidate_count=%s,trial_count=%s,issue_count=%s,heartbeat_at=now()
        where id=%s
        """,
        (row["candidates"], row["completed"], trials["n"], issues["n"], run_id),
    )


def _write_metric(run_id: str, split: str, segment: str, cost_bps: int, recipe: str, metrics: dict[str, Any], p_value: float, q_value: float) -> None:
    execute(
        """
        insert into public.dip_trigger_metrics(
          run_id,split,segment_key,recipe_key,cost_bps,observations,independent_dates,symbols,
          mean_net_return_pct,median_net_return_pct,win_rate_pct,net_target_success_rate_pct,
          net_target_wilson_low_pct,loss_5pct_rate_pct,positive_date_rate_pct,positive_symbol_rate_pct,
          worst_fold_mean_pct,profit_factor,bootstrap_ci_low_pct,bootstrap_ci_high_pct,p_value,q_value,metrics_json
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (run_id,split,segment_key,recipe_key,cost_bps) do update set
          observations=excluded.observations,independent_dates=excluded.independent_dates,symbols=excluded.symbols,
          mean_net_return_pct=excluded.mean_net_return_pct,median_net_return_pct=excluded.median_net_return_pct,
          win_rate_pct=excluded.win_rate_pct,net_target_success_rate_pct=excluded.net_target_success_rate_pct,
          net_target_wilson_low_pct=excluded.net_target_wilson_low_pct,loss_5pct_rate_pct=excluded.loss_5pct_rate_pct,
          positive_date_rate_pct=excluded.positive_date_rate_pct,positive_symbol_rate_pct=excluded.positive_symbol_rate_pct,
          worst_fold_mean_pct=excluded.worst_fold_mean_pct,profit_factor=excluded.profit_factor,
          bootstrap_ci_low_pct=excluded.bootstrap_ci_low_pct,bootstrap_ci_high_pct=excluded.bootstrap_ci_high_pct,
          p_value=excluded.p_value,q_value=excluded.q_value,metrics_json=excluded.metrics_json,created_at=now()
        """,
        (
            run_id, split, segment, recipe, cost_bps, metrics.get("observations", 0), metrics.get("independent_dates"), metrics.get("symbols"),
            metrics.get("mean_net_return_pct"), metrics.get("median_net_return_pct"), metrics.get("win_rate_pct"),
            metrics.get("net_target_success_rate_pct"), metrics.get("net_target_wilson_low_pct"), metrics.get("loss_5pct_rate_pct"),
            metrics.get("positive_date_rate_pct"), metrics.get("positive_symbol_rate_pct"), metrics.get("worst_fold_mean_pct"),
            metrics.get("profit_factor"), metrics.get("bootstrap_ci_low_pct"), metrics.get("bootstrap_ci_high_pct"), p_value, q_value, Jsonb(metrics),
        ),
    )


def _trial_frame(run_id: str, sealed_opened: bool) -> pd.DataFrame:
    rows = fetch_all(
        """
        select symbol,trade_date,split,recipe_key,trigger_at,entry_at,entry_price,target_hit,stop_hit,
               gross_return_pct,max_gain_pct,max_drawdown_pct,trigger_features
        from public.dip_trigger_trials
        where run_id=%s and (%s or split <> 'sealed_test')
        order by trade_date,symbol,recipe_key
        """,
        (run_id, sealed_opened),
    )
    return pd.DataFrame(rows)


def _segment_frame(frame: pd.DataFrame, segment: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    if segment == "all":
        return frame
    return frame[frame["entry_price"].map(lambda value: segment_matches(float(value), segment))]


def _outcome_evidence_counts(run_id: str) -> dict[tuple[str, str], int]:
    rows = fetch_all(
        """
        select split,flag,count(*) as n
        from public.dip_trigger_candidates c
        cross join lateral jsonb_array_elements_text(c.quality_flags) as flags(flag)
        where c.run_id=%s and (flag like 'unresolved_outcome_%%' or flag like 'incomplete_close_path_%%')
        group by split,flag
        """, (run_id,),
    )
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        recipe = str(row["flag"]).removeprefix("unresolved_outcome_").removeprefix("incomplete_close_path_")
        key = (str(row["split"]), recipe)
        counts[key] = counts.get(key, 0) + int(row["n"])
    return counts


def _with_outcome_evidence(metrics: dict[str, Any], counts: dict[tuple[str, str], int], split: str, recipe: str) -> dict[str, Any]:
    return {**metrics, "unresolved_outcomes": counts.get((split, recipe), 0), "evidence_version": EVIDENCE_VERSION}


def _compute_metrics(run: dict[str, Any]) -> tuple[str | None, str | None, str, dict[str, Any]]:
    costs = sorted({int(x) for x in run["cost_bps"]})
    selection_cost = max(costs)
    frame = _trial_frame(str(run["id"]), bool(run.get("sealed_opened")))

    if run.get("run_mode") in {"forward_sealed", "historical_sealed"}:
        return _compute_confirmation_metrics(run, frame, selection_cost)
    outcome_counts = _outcome_evidence_counts(str(run["id"]))

    if run.get("sealed_opened"):
        winner = run.get("winner_recipe")
        segment = str(run.get("winner_segment") or "all")
        execute("delete from public.dip_trigger_metrics where run_id=%s and split='sealed_test'", (run["id"],))
        if not winner or frame.empty:
            return winner, segment, "sealed_test_unavailable", {"reason": "No frozen winner or no sealed trials"}
        sealed = _segment_frame(frame[(frame["split"] == "sealed_test") & (frame["recipe_key"] == winner)], segment)
        metrics = performance_metrics(sealed, selection_cost, settings.bootstrap_iterations, float(run["target_net_pct"])) if not sealed.empty else {"observations": 0}
        metrics = _with_outcome_evidence(metrics, outcome_counts, "sealed_test", str(winner))
        p_value = float(metrics.get("bootstrap_p_one_sided") if metrics.get("bootstrap_p_one_sided") is not None else 1.0)
        _write_metric(str(run["id"]), "sealed_test", segment, selection_cost, str(winner), metrics, p_value, p_value)
        strong = materially_consistent(metrics, p_value, True)
        promising = materially_consistent(metrics, p_value, False)
        compelling = compelling_small_sample(metrics, p_value)
        verdict = "validated_for_paper_testing" if strong else ("promising_but_unproven" if promising else ("compelling_but_not_validated" if compelling else "rejected"))
        return str(winner), segment, verdict, {
            "evidence_version": EVIDENCE_VERSION,
            "selection_cost_bps": selection_cost,
            "sealed_test_opened": True,
            "sealed_test_processed": True,
            "sealed_metrics": metrics,
            "policy": "Only the frozen validation recipe and segment were evaluated in the sealed split.",
        }

    execute("delete from public.dip_trigger_metrics where run_id=%s and split in ('discovery','validation')", (run["id"],))
    if frame.empty or frame[frame["split"].isin(["discovery", "validation"])].empty:
        return None, None, "rejected_no_trials", {"reason": "No trigger recipe produced an executable trial"}

    records: list[dict[str, Any]] = []
    for split in ("discovery", "validation"):
        split_frame = frame[frame["split"] == split]
        for cost in costs:
            groups: list[dict[str, Any]] = []
            p_values: list[float] = []
            for segment in SEGMENTS:
                segmented = _segment_frame(split_frame, segment)
                for recipe in sorted(segmented["recipe_key"].unique()) if not segmented.empty else []:
                    subset = segmented[segmented["recipe_key"] == recipe]
                    metrics = performance_metrics(subset, cost, settings.bootstrap_iterations, float(run["target_net_pct"]))
                    metrics = _with_outcome_evidence(metrics, outcome_counts, split, recipe)
                    p_value = float(metrics.get("bootstrap_p_one_sided") if metrics.get("bootstrap_p_one_sided") is not None else 1.0)
                    groups.append({"split": split, "segment_key": segment, "cost_bps": cost, "recipe_key": recipe, "metrics": metrics, "p_value": p_value})
                    p_values.append(p_value)
            q_values = benjamini_hochberg(p_values)
            for record, q_value in zip(groups, q_values):
                record["q_value"] = q_value
                records.append(record)
                _write_metric(str(run["id"]), split, record["segment_key"], cost, record["recipe_key"], record["metrics"], record["p_value"], q_value)

    validation = [r for r in records if r["split"] == "validation" and r["cost_bps"] == selection_cost]
    discovery_lookup = {
        (r["segment_key"], r["recipe_key"]): r
        for r in records
        if r["split"] == "discovery" and r["cost_bps"] == selection_cost
    }

    def discovery_supports(record: dict[str, Any]) -> bool:
        discovery = discovery_lookup.get((record["segment_key"], record["recipe_key"]))
        if not discovery:
            return False
        metrics = discovery["metrics"]
        return (
            not any(metrics.get(key, 0) for key in ("unresolved_outcomes", "invalid_outcomes", "legacy_evidence_observations"))
            and
            metrics.get("observations", 0) >= 12
            and metrics.get("independent_dates", 0) >= 3
            and metrics.get("symbols", 0) >= 5
            and (metrics.get("mean_net_return_pct") or -999) >= 0.25
            and (metrics.get("median_net_return_pct") or -999) >= 0
            and (metrics.get("positive_date_rate_pct") or 0) >= 60
            and (metrics.get("positive_symbol_rate_pct") or 0) >= 50
            and (metrics.get("worst_fold_mean_pct") if metrics.get("worst_fold_mean_pct") is not None else -999) >= 0
            and (metrics.get("profit_factor") or 0) >= 1.20
        )

    cross_split_validation = [record for record in validation if discovery_supports(record)]
    winner, _eligible_diagnostic, tier = select_validation_record(cross_split_validation)
    _none, diagnostic, _none_tier = select_validation_record(validation)
    winner_key = winner["recipe_key"] if winner else None
    winner_segment = winner["segment_key"] if winner else None
    verdict = (
        "validation_passed_pending_sealed_test" if tier == "strong"
        else "promising_but_unproven_pending_sealed_test" if tier == "promising"
        else "compelling_small_sample_pending_sealed_test" if tier == "compelling_small_sample"
        else "rejected_no_materially_consistent_trigger"
    )
    result = {
        "evidence_version": EVIDENCE_VERSION,
        "selection_cost_bps": selection_cost,
        "winner_recipe": winner_key,
        "winner_segment": winner_segment,
        "winner_validation_metrics": winner["metrics"] if winner else None,
        "best_validation_recipe_diagnostic_only": diagnostic["recipe_key"] if diagnostic else None,
        "best_validation_segment_diagnostic_only": diagnostic["segment_key"] if diagnostic else None,
        "best_validation_metrics_diagnostic_only": diagnostic["metrics"] if diagnostic else None,
        "validation_records_with_discovery_support": len(cross_split_validation),
        "selection_tier": tier,
        "sealed_test_opened": False,
        "policy": "Clock time is descriptive only. A recipe must first show non-negative, diversified discovery performance, then pass the stricter validation consistency gates at the highest cost assumption.",
    }
    return winner_key, winner_segment, verdict, result


def _confirmation_target_sessions(run: dict[str, Any]) -> int:
    return int(run.get("confirmation_target_sessions") or run.get("forward_target_sessions") or CONFIRMATION_INITIAL_SESSIONS)


def _confirmation_anchor(run: dict[str, Any]) -> date:
    value = run.get("confirmation_anchor_date")
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return run["start_date"] if run.get("run_mode") == "forward_sealed" else run["end_date"]


def _verify_frozen_confirmation(run: dict[str, Any], recipe: str, segment: str) -> None:
    verify_frozen_evidence_contract(run, recipe, segment)  # v2.1 wording: Frozen forward configuration integrity check failed


async def _resolve_calendar_for_run(run: dict[str, Any], alpaca: AlpacaClient) -> list[dict[str, Any]] | None:
    mode = str(run.get("run_mode") or "discovery")
    if mode not in {"forward_sealed", "historical_sealed"}:
        return await alpaca.calendar(run["start_date"], run["end_date"])

    target_sessions = _confirmation_target_sessions(run)
    if target_sessions not in {CONFIRMATION_INITIAL_SESSIONS, CONFIRMATION_MAX_SESSIONS}:
        raise RuntimeError("Confirmation target sessions must be 30 or 90")
    recipe = str(run.get("winner_recipe") or "")
    segment = str(run.get("winner_segment") or "")
    if not recipe or not segment:
        raise RuntimeError("Confirmation run is missing its frozen recipe or segment")
    _verify_frozen_confirmation(run, recipe, segment)
    anchor = _confirmation_anchor(run)

    if mode == "forward_sealed":
        today_et = datetime.now(NY).date()
        completed_through = today_et - timedelta(days=1)
        probe_end = min(anchor + timedelta(days=target_sessions * 2 + 45), completed_through)
        calendar: list[dict[str, Any]] = []
        if probe_end >= anchor:
            calendar = await alpaca.calendar(anchor, probe_end)
        eligible = sorted(
            [row for row in calendar if anchor <= date.fromisoformat(str(row["date"])) < today_et],
            key=lambda row: str(row["date"]),
        )
        if len(eligible) < target_sessions:
            available = len(eligible)
            latest = date.fromisoformat(str(eligible[-1]["date"])) if eligible else anchor
            execute(
                """
                update public.dip_trigger_runs
                set status='completed_with_warnings',stage='forward_window_waiting_for_data',
                    verdict='forward_window_incomplete',start_date=%s,end_date=%s,completed_at=now(),heartbeat_at=now(),
                    result_json=coalesce(result_json,'{}'::jsonb) || jsonb_build_object(
                      'confirmation_target_sessions',%s::int,'completed_sessions_available',%s::int,
                      'sessions_still_required',%s::int,'latest_completed_session',%s::text,
                      'confirmation_window_complete',false
                    )
                where id=%s
                """,
                (anchor, latest, target_sessions, available, target_sessions - available, latest.isoformat(), run["id"]),
            )
            return None
        selected = eligible[:target_sessions]
    else:
        parent = fetch_one("select start_date from public.dip_trigger_runs where id=%s", (run.get("parent_run_id"),))
        if not parent:
            raise RuntimeError("Historical confirmation parent run is missing")
        parent_start = parent["start_date"]
        if anchor >= parent_start:
            raise RuntimeError("Historical sealed window overlaps the discovery period")
        probe_start = anchor - timedelta(days=target_sessions * 3 + 60)
        calendar = await alpaca.calendar(probe_start, anchor)
        eligible = sorted(
            [row for row in calendar if date.fromisoformat(str(row["date"])) <= anchor and date.fromisoformat(str(row["date"])) < parent_start],
            key=lambda row: str(row["date"]),
        )
        if len(eligible) < target_sessions:
            available = len(eligible)
            execute(
                """
                update public.dip_trigger_runs
                set status='completed_with_warnings',stage='historical_window_insufficient',
                    verdict='backtest_window_insufficient',completed_at=now(),heartbeat_at=now(),
                    result_json=coalesce(result_json,'{}'::jsonb) || jsonb_build_object(
                      'confirmation_target_sessions',%s::int,'historical_sessions_available',%s::int,
                      'sessions_still_required',%s::int,'confirmation_window_complete',false
                    )
                where id=%s
                """,
                (target_sessions, available, target_sessions - available, run["id"]),
            )
            return None
        # The initial test is the 30 sessions immediately preceding the chosen anchor.
        # Extending to 90 adds the preceding 60 while preserving those original 30.
        selected = eligible[-target_sessions:]

    actual_start = date.fromisoformat(str(selected[0]["date"]))
    actual_end = date.fromisoformat(str(selected[-1]["date"]))
    prefix = "forward" if mode == "forward_sealed" else "backtest"
    stage = f"{prefix}_{target_sessions}_processing"
    execute(
        """
        update public.dip_trigger_runs
        set start_date=%s,end_date=%s,confirmation_stage=%s,forward_stage=%s,
            result_json=coalesce(result_json,'{}'::jsonb) || jsonb_build_object(
              'confirmation_target_sessions',%s::int,'confirmation_start_date',%s::text,'confirmation_end_date',%s::text,
              'confirmation_window_complete',true,'confirmation_mode',%s::text
            )
        where id=%s
        """,
        (actual_start, actual_end, stage, stage, target_sessions, actual_start.isoformat(), actual_end.isoformat(), mode, run["id"]),
    )
    run["start_date"] = actual_start
    run["end_date"] = actual_end
    run["confirmation_stage"] = stage
    return selected


def _compute_confirmation_metrics(run: dict[str, Any], frame: pd.DataFrame, selection_cost: int) -> tuple[str, str, str, dict[str, Any]]:
    winner = str(run.get("winner_recipe") or "")
    segment = str(run.get("winner_segment") or "all")
    execute("delete from public.dip_trigger_metrics where run_id=%s and split='sealed_test'", (run["id"],))
    frozen = _segment_frame(frame[(frame["split"] == "sealed_test") & (frame["recipe_key"] == winner)], segment) if not frame.empty else frame
    metrics = performance_metrics(frozen, selection_cost, settings.bootstrap_iterations, float(run["target_net_pct"])) if not frozen.empty else {"observations": 0}
    metrics = _with_outcome_evidence(metrics, _outcome_evidence_counts(str(run["id"])), "sealed_test", winner)
    p_value = float(metrics.get("bootstrap_p_one_sided") if metrics.get("bootstrap_p_one_sided") is not None else 1.0)
    _write_metric(str(run["id"]), "sealed_test", segment, selection_cost, winner, metrics, p_value, p_value)
    target_sessions = _confirmation_target_sessions(run)
    mode = str(run.get("run_mode"))
    verdict, passed, gate = evaluate_confirmation_gate(metrics, target_sessions, mode)
    result = dict(run.get("result_json") or {})
    policy = (
        "True-forward evidence: all sessions occur strictly after the parent research period."
        if mode == "forward_sealed"
        else "Historical sealed evidence: an earlier non-overlapping block tests the frozen rule without re-selection."
    )
    result.update({
        "evidence_version": EVIDENCE_VERSION,
        "selection_cost_bps": selection_cost,
        "confirmation_target_sessions": target_sessions,
        "confirmation_metrics": metrics,
        "confirmation_gate": gate,
        "confirmation_gate_passed": passed,
        "forward_target_sessions": target_sessions if mode == "forward_sealed" else None,
        "forward_metrics": metrics if mode == "forward_sealed" else None,
        "forward_gate": gate if mode == "forward_sealed" else None,
        "frozen_recipe": winner,
        "frozen_segment": segment,
        "backtest_scope": run.get("backtest_scope"),
        "policy": policy + " No recipe, segment, threshold or cost optimisation occurs.",
    })
    return winner, segment, verdict, result


# Backwards-compatible name retained for older imports.
def _compute_forward_metrics(run: dict[str, Any], frame: pd.DataFrame, selection_cost: int) -> tuple[str, str, str, dict[str, Any]]:
    return _compute_confirmation_metrics(run, frame, selection_cost)


async def process_run(run: dict[str, Any], alpaca: AlpacaClient) -> None:
    run_id = str(run["id"])
    logger.info("Processing trigger run %s (%s)", run_id, run["name"])
    try:
        _heartbeat(run_id, "loading_market_calendar")
        calendar = await _resolve_calendar_for_run(run, alpaca)
        if calendar is None:
            _update_runtime("idle")
            return
        sessions = _calendar_sessions(calendar)
        if not sessions:
            raise RuntimeError("Alpaca returned no market sessions")

        _heartbeat(run_id, "generating_candidates")
        count = fetch_one("select count(*) as n from public.dip_trigger_candidates where run_id=%s", (run_id,)) or {"n": 0}
        should_generate = run.get("run_mode") in {"forward_sealed", "historical_sealed"} or int(count["n"]) == 0
        if should_generate:
            if run["source_mode"] == "scanner_alerts":
                _scanner_alert_candidates(run)
            elif run["source_mode"] == "manual_symbols":
                _manual_symbol_candidates(run, sessions)
            elif run["source_mode"] != "candidate_csv":
                raise RuntimeError(f"Unsupported source mode: {run['source_mode']}")
        _sync_calendar(run, sessions)
        _assign_splits(run)
        _refresh_counts(run_id)

        candidates = fetch_all(
            """
            select * from public.dip_trigger_candidates
            where run_id=%s and status in ('queued','failed')
            order by trade_date,symbol
            """,
            (run_id,),
        )
        benchmark_cache: dict[date, pd.DataFrame] = {}
        for index, candidate in enumerate(candidates, start=1):
            if _cancel_requested(run_id):
                execute("update public.dip_trigger_runs set status='cancelled',stage='cancelled',completed_at=now() where id=%s", (run_id,))
                _update_runtime("idle")
                return
            _heartbeat(run_id, f"processing_candidates_{index}_of_{len(candidates)}")
            await _process_candidate(run, candidate, alpaca, benchmark_cache)
            if index % 10 == 0 or index == len(candidates):
                _refresh_counts(run_id)

        _heartbeat(run_id, "computing_material_consistency")
        winner, segment, verdict, result = _compute_metrics(run)
        _refresh_counts(run_id)
        failed = fetch_one("select count(*) as n from public.dip_trigger_candidates where run_id=%s and status='failed'", (run_id,)) or {"n": 0}
        status = "completed_with_warnings" if int(failed["n"]) else "completed"
        if run.get("run_mode") in {"forward_sealed", "historical_sealed"}:
            prefix = "forward" if run.get("run_mode") == "forward_sealed" else "backtest"
            target = _confirmation_target_sessions(run)
            pass_verdict = f"{prefix}_30_pass_extension_available"
            stage = f"{prefix}_30_complete_extension_available" if verdict == pass_verdict else f"{prefix}_{target}_complete"
        else:
            stage = "completed_sealed_test" if run.get("sealed_opened") else ("awaiting_sealed_test" if winner else "completed_no_consistent_trigger")
        execute(
            """
            update public.dip_trigger_runs
            set status=%s,stage=%s,winner_recipe=%s,winner_segment=%s,verdict=%s,result_json=%s,
                completed_at=now(),heartbeat_at=now(),last_error=null
            where id=%s
            """,
            (status, stage, winner, segment, verdict, Jsonb(result), run_id),
        )
        logger.info("Completed trigger run %s: %s winner=%s", run_id, verdict, winner)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Trigger run failed: %s", run_id)
        execute("update public.dip_trigger_runs set status='failed',stage='failed',last_error=%s,completed_at=now(),heartbeat_at=now() where id=%s", (message[:4000], run_id))
        _issue(run_id, "process_run", message, "error")
        _update_runtime("error", run_id, message[:4000])
        return
    _update_runtime("idle")


async def worker_loop() -> None:
    settings.validate_worker()
    _update_runtime("starting")
    _recover_stale_work()
    alpaca = AlpacaClient()
    logger.info("Dip-reversal trigger worker started; feed=%s", settings.alpaca_feed)
    try:
        while True:
            _update_runtime("idle")
            run = fetch_one("select * from public.claim_next_dip_trigger_run()")
            if run:
                _update_runtime("running", str(run["id"]))
                await process_run(run, alpaca)
            else:
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await alpaca.close()


if __name__ == "__main__":
    asyncio.run(worker_loop())
