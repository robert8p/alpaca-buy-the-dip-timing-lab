from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
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
    assign_chronological_splits,
    bars_to_frame,
    benjamini_hochberg,
    candidate_features,
    confirmation_entry,
    fixed_entry,
    passes_dip_gate,
    performance_metrics,
    latest_candidate_availability,
    job_marks_historical_calibration,
    scheduled_midday_scanner_cutoff,
    segment_matches,
    select_validation_record,
    simulate_trial,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dip-worker")


def _run_config(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "min_price": run["min_price"],
        "max_price": run["max_price"],
        "min_dollar_volume": run["min_dollar_volume"],
        "dip_from_high_pct": run["dip_from_high_pct"],
        "dip_from_open_pct": run["dip_from_open_pct"],
        "require_below_vwap": run["require_below_vwap"],
    }


def _cutoff_for(trade_date: date, cutoff: time) -> datetime:
    return datetime.combine(trade_date, cutoff, tzinfo=NY)


def _issue(
    run_id: str,
    stage: str,
    message: str,
    severity: str = "warning",
    candidate_id: str | None = None,
    symbol: str | None = None,
    trade_date: date | None = None,
) -> None:
    execute(
        """
        insert into public.dip_issues(run_id, candidate_id, symbol, trade_date, stage, severity, message)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (run_id, candidate_id, symbol, trade_date, stage, severity, message[:4000]),
    )


def _update_runtime(status: str, run_id: str | None = None, error: str | None = None) -> None:
    execute(
        """
        update public.dip_runtime
        set version=%s, worker_status=%s, active_run_id=%s, heartbeat_at=now(), last_error=%s, updated_at=now()
        where id=1
        """,
        (__version__, status, run_id, error),
    )


def _recover_stale_work() -> None:
    """Recover work abandoned by a killed/redeployed single worker."""
    execute(
        """
        update public.dip_candidates as c
        set status='queued', retry_count=c.retry_count+1,
            last_error=coalesce(c.last_error || '; ', '') || 'requeued_after_stale_worker'
        from public.dip_runs as r
        where c.run_id=r.id
          and c.status='running'
          and r.status='running'
          and coalesce(r.heartbeat_at, r.started_at, r.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )
    execute(
        """
        update public.dip_runs as dr
        set status=case when dr.cancel_requested then 'cancelled' else 'queued' end,
            stage=case when dr.cancel_requested then 'cancelled_after_worker_restart' else 'recovered_after_worker_restart' end,
            retry_count=dr.retry_count+1,
            last_error=case when dr.cancel_requested then dr.last_error else coalesce(dr.last_error || '; ', '') || 'worker heartbeat became stale; run requeued' end,
            completed_at=case when dr.cancel_requested then now() else dr.completed_at end
        where dr.status='running'
          and coalesce(dr.heartbeat_at, dr.started_at, dr.created_at) < now() - make_interval(mins => %s)
        """,
        (settings.stale_work_minutes,),
    )


def _heartbeat(run_id: str, stage: str | None = None) -> None:
    if stage:
        execute("update public.dip_runs set heartbeat_at=now(), stage=%s where id=%s", (stage, run_id))
    else:
        execute("update public.dip_runs set heartbeat_at=now() where id=%s", (run_id,))
    _update_runtime("running", run_id)


def _cancel_requested(run_id: str) -> bool:
    row = fetch_one("select cancel_requested from public.dip_runs where id=%s", (run_id,))
    return bool(row and row["cancel_requested"])


def _insert_candidates(
    run: dict[str, Any],
    rows: list[tuple[str, date, datetime, str, str | None, datetime | None]],
) -> None:
    if not rows:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into public.dip_candidates(
                  run_id, symbol, trade_date, cutoff_at, source, source_alert_id, source_available_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id, symbol, trade_date) do nothing
                """,
                [
                    (run["id"], symbol, trade_date, cutoff, source, source_id, source_available_at)
                    for symbol, trade_date, cutoff, source, source_id, source_available_at in rows
                ],
            )
        conn.commit()


def _scanner_alert_candidates(run: dict[str, Any]) -> int:
    exists = fetch_one("select to_regclass('public.live_signal_alerts') as table_name")
    if not exists or not exists["table_name"]:
        raise RuntimeError("public.live_signal_alerts was not found. Use manual symbols or candidate CSV, or deploy into the scanner Supabase project.")
    columns = {
        row["column_name"]
        for row in fetch_all(
            """
            select column_name from information_schema.columns
            where table_schema='public' and table_name='live_signal_alerts'
            """
        )
    }
    required = {"id", "trade_date", "symbol", "scan_type"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError("live_signal_alerts is missing required columns: " + ", ".join(missing))

    # Candidate availability must be known before the research cutoff. The live
    # scanner's original schema records the alert persistence time in
    # first_alerted_at and the scheduled/logical scan time in
    # live_scan_jobs.cutoff_at. For live rows, use the latest available audit
    # timestamp. For historical calibration rows, use only logical scan-time
    # fields so a backfill's current insert time does not invalidate the
    # historical candidate.
    alert_availability_columns = [
        name
        for name in ("cutoff_at", "decision_at", "created_at", "first_alerted_at")
        if name in columns
    ]

    job_join = ""
    job_availability_key: str | None = None
    job_source_projection = "null::text as job_source"
    job_parameters_projection = "'{}'::jsonb as job_parameters"
    if "job_id" in columns:
        job_exists = fetch_one("select to_regclass('public.live_scan_jobs') as table_name")
        if job_exists and job_exists["table_name"]:
            job_columns = {
                row["column_name"]
                for row in fetch_all(
                    """
                    select column_name from information_schema.columns
                    where table_schema='public' and table_name='live_scan_jobs'
                    """
                )
            }
            if {"id", "cutoff_at"}.issubset(job_columns):
                job_join = "left join public.live_scan_jobs as j on j.id = a.job_id"
                job_availability_key = "job_cutoff_at"
                job_source_projection = (
                    "j.source::text as job_source" if "source" in job_columns else "null::text as job_source"
                )
                job_parameters_projection = (
                    "j.parameters as job_parameters" if "parameters" in job_columns else "'{}'::jsonb as job_parameters"
                )
            else:
                job_source_projection = "null::text as job_source"
                job_parameters_projection = "'{}'::jsonb as job_parameters"
        else:
            job_source_projection = "null::text as job_source"
            job_parameters_projection = "'{}'::jsonb as job_parameters"
    else:
        job_source_projection = "null::text as job_source"
        job_parameters_projection = "'{}'::jsonb as job_parameters"

    availability_keys = list(alert_availability_columns)
    if job_availability_key:
        availability_keys.append(job_availability_key)
    if not availability_keys:
        raise RuntimeError(
            "live_signal_alerts has no auditable first_alerted/cutoff/decision/created timestamp "
            "and no compatible live_scan_jobs.cutoff_at; scanner-alert candidates are disabled "
            "to prevent look-ahead bias"
        )

    availability_projection_parts = [
        f"a.{name} as availability_{name}" for name in alert_availability_columns
    ]
    if job_availability_key:
        availability_projection_parts.append(
            "j.cutoff_at as availability_job_cutoff_at"
        )
    availability_projection = ",\n               ".join(availability_projection_parts)

    decision_projection = (
        "a.decision::text as source_decision"
        if "decision" in columns
        else "null::text as source_decision"
    )
    decision_filter = (
        "and a.decision is distinct from 'reject'" if "decision" in columns else ""
    )
    security_filter = (
        "and a.security_eligible is distinct from false"
        if "security_eligible" in columns
        else ""
    )
    alerts = fetch_all(
        f"""
        select a.id::text as source_alert_id, a.trade_date,
               upper(a.symbol) as symbol,
               {decision_projection},
               {job_source_projection},
               {job_parameters_projection},
               {availability_projection}
        from public.live_signal_alerts as a
        {job_join}
        where a.trade_date between %s and %s
          and a.scan_type = 'midday'
          {decision_filter}
          {security_filter}
        order by a.trade_date, a.symbol
        """,
        (run["start_date"], run["end_date"]),
    )
    retained: dict[tuple[str, date], tuple[str, date, datetime, str, str | None, datetime | None]] = {}
    excluded = 0
    duplicates = 0
    for row in alerts:
        research_cutoff = _cutoff_for(row["trade_date"], run["cutoff_et"])
        is_calibration = job_marks_historical_calibration(row)
        if is_calibration:
            logical_keys = [
                name
                for name in availability_keys
                if name not in {"created_at", "first_alerted_at"}
            ]
            available_at = latest_candidate_availability(
                [row.get(f"availability_{name}") for name in logical_keys],
                row["trade_date"],
            )
            if available_at is None:
                available_at = scheduled_midday_scanner_cutoff(row["trade_date"])
            source = "live_signal_alerts_midday_calibration"
        else:
            available_at = latest_candidate_availability(
                [row.get(f"availability_{name}") for name in availability_keys],
                row["trade_date"],
            )
            source = "live_signal_alerts_midday"
        if available_at is None or available_at > research_cutoff:
            excluded += 1
            _issue(
                str(run["id"]),
                "candidate_availability",
                f"Excluded scanner alert: source available at {available_at}, after research cutoff {research_cutoff}",
                "warning",
                symbol=row["symbol"],
                trade_date=row["trade_date"],
            )
            continue
        candidate_row = (
            row["symbol"], row["trade_date"], research_cutoff, source,
            row["source_alert_id"], available_at,
        )
        key = (row["symbol"], row["trade_date"])
        existing = retained.get(key)
        if existing is not None:
            duplicates += 1
        if existing is None or (available_at is not None and (existing[5] is None or available_at < existing[5])):
            retained[key] = candidate_row
    rows = sorted(retained.values(), key=lambda value: (value[1], value[0]))
    _insert_candidates(run, rows)
    logger.info(
        "Scanner-alert source retained %s unique candidates, excluded %s late candidates and collapsed %s duplicates",
        len(rows), excluded, duplicates,
    )
    return len(rows)


def _calendar_sessions(calendar: list[dict[str, Any]]) -> dict[date, tuple[datetime, datetime]]:
    sessions: dict[date, tuple[datetime, datetime]] = {}
    for row in calendar:
        trade_date = date.fromisoformat(str(row["date"]))
        open_text = str(row.get("open") or "09:30")[:5]
        close_text = str(row.get("close") or "16:00")[:5]
        session_open = datetime.combine(trade_date, datetime.strptime(open_text, "%H:%M").time(), tzinfo=NY)
        session_close = datetime.combine(trade_date, datetime.strptime(close_text, "%H:%M").time(), tzinfo=NY)
        sessions[trade_date] = (session_open, session_close)
    return sessions


def _manual_symbol_candidates(
    run: dict[str, Any],
    sessions: dict[date, tuple[datetime, datetime]],
) -> int:
    symbols = sorted({str(x).strip().upper() for x in (run["symbols"] or []) if str(x).strip()})
    if not symbols:
        raise RuntimeError("No symbols were supplied")
    rows = [
        (symbol, trade_date, _cutoff_for(trade_date, run["cutoff_et"]), "manual_symbols", None, None)
        for trade_date in sorted(sessions)
        for symbol in symbols
    ]
    _insert_candidates(run, rows)
    return len(rows)


def _sync_market_calendar(
    run: dict[str, Any],
    sessions: dict[date, tuple[datetime, datetime]],
) -> None:
    execute_many(
        """
        update public.dip_candidates
        set session_open_at=%s, session_close_at=%s
        where run_id=%s and trade_date=%s
        """,
        [
            (session_open, session_close, run["id"], trade_date)
            for trade_date, (session_open, session_close) in sessions.items()
        ],
    )
    execute(
        """
        update public.dip_candidates
        set status='skipped', last_error='no_market_session',
            quality_flags='["no_market_session"]'::jsonb, completed_at=now()
        where run_id=%s and session_open_at is null and status='queued'
        """,
        (run["id"],),
    )


def _assign_splits(run: dict[str, Any]) -> None:
    rows = fetch_all(
        """
        select distinct trade_date from public.dip_candidates
        where run_id=%s and session_open_at is not null and status <> 'skipped'
        order by trade_date
        """,
        (run["id"],),
    )
    mapping = assign_chronological_splits(
        [row["trade_date"] for row in rows],
        float(run["discovery_ratio"]),
        float(run["validation_ratio"]),
    )
    with connection() as conn:
        with conn.cursor() as cur:
            for trade_date, split in mapping.items():
                cur.execute(
                    "update public.dip_candidates set split=%s where run_id=%s and trade_date=%s",
                    (split, run["id"], trade_date),
                )
        conn.commit()
    if not bool(run.get("sealed_opened")):
        execute(
            """
            update public.dip_candidates
            set status='sealed'
            where run_id=%s and split='sealed_test' and status='queued'
            """,
            (run["id"],),
        )


def _insert_trials(run: dict[str, Any], candidate: dict[str, Any], results: list[Any]) -> None:
    if not results:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into public.dip_trials(
                  run_id, candidate_id, symbol, trade_date, split, variant_key,
                  entry_at, entry_price, entry_reason, target_price, stop_price,
                  target_hit, first_target_hit_at, stop_hit, first_stop_hit_at,
                  exit_at, exit_price, exit_reason, gross_return_pct,
                  max_gain_pct, max_drawdown_pct, same_bar_ambiguous
                ) values (
                  %s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,
                  %s,%s,%s,%s,
                  %s,%s,%s,%s,
                  %s,%s,%s
                )
                on conflict (candidate_id, variant_key) do nothing
                """,
                [
                    (
                        run["id"], candidate["id"], candidate["symbol"], candidate["trade_date"],
                        candidate["split"], result.variant_key, result.entry_at, result.entry_price,
                        result.entry_reason, result.target_price, result.stop_price, result.target_hit,
                        result.first_target_hit_at, result.stop_hit, result.first_stop_hit_at,
                        result.exit_at, result.exit_price, result.exit_reason, result.gross_return_pct,
                        result.max_gain_pct, result.max_drawdown_pct, result.same_bar_ambiguous,
                    )
                    for result in results
                ],
            )
        conn.commit()


async def _process_candidate(run: dict[str, Any], candidate: dict[str, Any], alpaca: AlpacaClient) -> None:
    candidate_id = str(candidate["id"])
    execute(
        "update public.dip_candidates set status='running', started_at=coalesce(started_at,now()), last_error=null where id=%s",
        (candidate_id,),
    )
    trade_date = candidate["trade_date"]
    session_open = candidate.get("session_open_at")
    session_close = candidate.get("session_close_at")
    if session_open is None or session_close is None:
        execute(
            """
            update public.dip_candidates
            set status='skipped', last_error='missing_market_calendar_session',
                quality_flags='["missing_market_calendar_session"]'::jsonb, completed_at=now()
            where id=%s
            """,
            (candidate_id,),
        )
        return
    start = session_open
    end = session_close + timedelta(minutes=1)
    try:
        raw = await alpaca.bars(candidate["symbol"], start, end)
        frame = bars_to_frame(raw, session_open, session_close)
        quality_flags: list[str] = []
        if len(frame) < 120:
            quality_flags.append("fewer_than_120_regular_session_bars")
        if frame.empty:
            execute(
                """
                update public.dip_candidates
                set status='skipped', bar_count=0, quality_flags=%s, last_error='no_regular_session_bars', completed_at=now()
                where id=%s
                """,
                (Jsonb(["no_regular_session_bars"]), candidate_id),
            )
            return
        expected_open_bar = pd.Timestamp(session_open)
        expected_signal_bar = pd.Timestamp(candidate["cutoff_at"] - timedelta(minutes=1))
        if frame.iloc[0]["timestamp"] != expected_open_bar:
            quality_flags.append("missing_exact_session_open_bar")
        signal_rows = frame[frame["timestamp"] < candidate["cutoff_at"]]
        if signal_rows.empty or signal_rows.iloc[-1]["timestamp"] != expected_signal_bar:
            quality_flags.append("missing_exact_pre_cutoff_bar")
        if (
            "missing_exact_pre_cutoff_bar" in quality_flags
            or (run["apply_dip_filter"] and "missing_exact_session_open_bar" in quality_flags)
        ):
            execute(
                """
                update public.dip_candidates
                set status='skipped', bar_count=%s, passed_dip_filter=false,
                    filter_reasons='["insufficient_point_in_time_bar_coverage"]'::jsonb,
                    quality_flags=%s, completed_at=now(), last_error='insufficient_point_in_time_bar_coverage'
                where id=%s
                """,
                (len(frame), Jsonb(sorted(set(quality_flags))), candidate_id),
            )
            return
        features = candidate_features(frame, candidate["cutoff_at"])
        passed, reasons = passes_dip_gate(features, _run_config(run))
        if not run["apply_dip_filter"]:
            passed = True
            reasons = []
        if not passed:
            execute(
                """
                update public.dip_candidates
                set status='skipped', bar_count=%s, candidate_features=%s,
                    passed_dip_filter=false, filter_reasons=%s, quality_flags=%s, completed_at=now()
                where id=%s
                """,
                (len(frame), Jsonb(features), Jsonb(reasons), Jsonb(quality_flags), candidate_id),
            )
            return

        if candidate["split"] == "sealed_test":
            winner_segment = str(run.get("winner_segment") or "all")
            if not segment_matches(features, winner_segment):
                quality_flags.append(f"outside_frozen_segment_{winner_segment}")
                execute(
                    """
                    update public.dip_candidates
                    set status='skipped', bar_count=%s, candidate_features=%s,
                        passed_dip_filter=true, filter_reasons='[]'::jsonb,
                        quality_flags=%s, completed_at=now(), last_error='outside_frozen_winner_segment'
                    where id=%s
                    """,
                    (len(frame), Jsonb(features), Jsonb(sorted(set(quality_flags))), candidate_id),
                )
                return

        retained_results: list[Any] = []
        expected_close_bar = pd.Timestamp(session_close - timedelta(minutes=1))

        def retain_result(result: Any) -> bool:
            if result.exit_reason == "market_close" and frame.iloc[-1]["timestamp"] != expected_close_bar:
                quality_flags.append(f"incomplete_close_path_{result.variant_key}")
                return False
            retained_results.append(result)
            return True

        winner_only = run.get("winner_variant") if candidate["split"] == "sealed_test" else None
        for hhmm in run["fixed_entry_times_et"]:
            variant_key = f"fixed_{str(hhmm).replace(':', '')}_et"
            if winner_only and variant_key != winner_only:
                continue
            found = fixed_entry(frame, trade_date, str(hhmm))
            if not found:
                quality_flags.append(f"no_entry_bar_fixed_{hhmm}")
                continue
            entry_idx, _row, reason = found
            result = simulate_trial(
                frame,
                variant_key,
                entry_idx,
                reason,
                float(run["target_gross_pct"]),
                float(run["stop_loss_pct"]),
            )
            retain_result(result)

        for confirmation in run["confirmation_variants"]:
            variant_key = f"confirm_{confirmation}"
            if winner_only and variant_key != winner_only:
                continue
            found = confirmation_entry(frame, candidate["cutoff_at"], str(confirmation))
            if not found:
                quality_flags.append(f"confirmation_not_reached_{confirmation}")
                continue
            entry_idx, _row, reason = found
            result = simulate_trial(
                frame,
                variant_key,
                entry_idx,
                reason,
                float(run["target_gross_pct"]),
                float(run["stop_loss_pct"]),
            )
            retain_result(result)

        _insert_trials(run, candidate, retained_results)
        created = len(retained_results)
        final_status = "completed" if created else "skipped"
        execute(
            """
            update public.dip_candidates
            set status=%s, bar_count=%s, candidate_features=%s,
                passed_dip_filter=true, filter_reasons='[]'::jsonb,
                quality_flags=%s, completed_at=now(), last_error=%s
            where id=%s
            """,
            (final_status, len(frame), Jsonb(features), Jsonb(sorted(set(quality_flags))), None if created else "no_entry_variants_available", candidate_id),
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        execute(
            """
            update public.dip_candidates
            set status='failed', retry_count=retry_count+1, last_error=%s, completed_at=now()
            where id=%s
            """,
            (message[:4000], candidate_id),
        )
        _issue(str(run["id"]), "process_candidate", message, "error", candidate_id, candidate["symbol"], trade_date)


def _refresh_counts(run_id: str) -> None:
    row = fetch_one(
        """
        select
          count(*) as candidates,
          count(*) filter (where status in ('completed','skipped','failed','sealed')) as completed,
          count(*) filter (where status='failed') as failed
        from public.dip_candidates where run_id=%s
        """,
        (run_id,),
    ) or {"candidates": 0, "completed": 0, "failed": 0}
    trials = fetch_one("select count(*) as n from public.dip_trials where run_id=%s", (run_id,)) or {"n": 0}
    issues = fetch_one("select count(*) as n from public.dip_issues where run_id=%s", (run_id,)) or {"n": 0}
    execute(
        """
        update public.dip_runs
        set candidate_count=%s, completed_candidate_count=%s, trial_count=%s, issue_count=%s, heartbeat_at=now()
        where id=%s
        """,
        (row["candidates"], row["completed"], trials["n"], issues["n"], run_id),
    )


def _write_metric(
    run_id: str,
    split: str,
    segment: str,
    cost_bps: int,
    variant: str,
    metrics: dict[str, Any],
    p_value: float,
    q_value: float,
) -> None:
    execute(
        """
        insert into public.dip_metrics(
          run_id, split, segment_key, variant_key, cost_bps, observations, independent_dates, symbols,
          mean_net_return_pct, median_net_return_pct, win_rate_pct,
          net_target_success_rate_pct, net_target_wilson_low_pct, net_target_daily_ci_low_pct, loss_5pct_rate_pct,
          target_before_stop_rate_pct, stop_before_target_rate_pct,
          bootstrap_ci_low_pct, bootstrap_ci_high_pct, p_value, q_value, metrics_json
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (run_id, split, segment_key, variant_key, cost_bps) do update set
          observations=excluded.observations,
          independent_dates=excluded.independent_dates,
          symbols=excluded.symbols,
          mean_net_return_pct=excluded.mean_net_return_pct,
          median_net_return_pct=excluded.median_net_return_pct,
          win_rate_pct=excluded.win_rate_pct,
          net_target_success_rate_pct=excluded.net_target_success_rate_pct,
          net_target_wilson_low_pct=excluded.net_target_wilson_low_pct,
          net_target_daily_ci_low_pct=excluded.net_target_daily_ci_low_pct,
          loss_5pct_rate_pct=excluded.loss_5pct_rate_pct,
          target_before_stop_rate_pct=excluded.target_before_stop_rate_pct,
          stop_before_target_rate_pct=excluded.stop_before_target_rate_pct,
          bootstrap_ci_low_pct=excluded.bootstrap_ci_low_pct,
          bootstrap_ci_high_pct=excluded.bootstrap_ci_high_pct,
          p_value=excluded.p_value,
          q_value=excluded.q_value,
          metrics_json=excluded.metrics_json,
          created_at=now()
        """,
        (
            run_id, split, segment, variant, cost_bps, metrics.get("observations", 0),
            metrics.get("independent_dates"), metrics.get("symbols"), metrics.get("mean_net_return_pct"),
            metrics.get("median_net_return_pct"), metrics.get("win_rate_pct"),
            metrics.get("net_target_success_rate_pct"), metrics.get("net_target_wilson_low_pct"),
            metrics.get("net_target_daily_ci_low_pct"), metrics.get("loss_5pct_rate_pct"),
            metrics.get("target_before_stop_rate_pct"),
            metrics.get("stop_before_target_rate_pct"),
            metrics.get("bootstrap_ci_low_pct"), metrics.get("bootstrap_ci_high_pct"),
            p_value, q_value, Jsonb(metrics),
        ),
    )


def _metric_positive(metrics: dict[str, Any], minimum_observations: int, minimum_dates: int) -> bool:
    return (
        metrics.get("observations", 0) >= minimum_observations
        and metrics.get("independent_dates", 0) >= minimum_dates
        and (metrics.get("mean_net_return_pct") if metrics.get("mean_net_return_pct") is not None else -999) > 0
        and (metrics.get("median_net_return_pct") if metrics.get("median_net_return_pct") is not None else -999) > 0
    )


SEGMENTS = ["all", "price_2_to_5", "price_5_to_20", "price_20_to_50"]


def _segment_frame(frame: pd.DataFrame, segment: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    price = pd.to_numeric(frame["cutoff_price"], errors="coerce")
    if segment == "all":
        return frame.copy()
    if segment == "price_2_to_5":
        return frame[(price >= 2.0) & (price < 5.0)].copy()
    if segment == "price_5_to_20":
        return frame[(price >= 5.0) & (price < 20.0)].copy()
    if segment == "price_20_to_50":
        return frame[(price >= 20.0) & (price <= 50.0)].copy()
    raise ValueError(f"Unknown segment: {segment}")


def _compute_metrics(run: dict[str, Any]) -> tuple[str | None, str | None, str, dict[str, Any]]:
    rows = fetch_all(
        """
        select t.symbol, t.trade_date, t.split, t.variant_key, t.entry_at, t.target_hit, t.stop_hit,
               t.same_bar_ambiguous, t.gross_return_pct, t.max_gain_pct, t.max_drawdown_pct,
               nullif(c.candidate_features->>'cutoff_price','')::double precision as cutoff_price
        from public.dip_trials t
        join public.dip_candidates c on c.id=t.candidate_id
        where t.run_id=%s
        order by trade_date, symbol, variant_key
        """,
        (run["id"],),
    )
    frame = pd.DataFrame(rows)
    costs = [int(x) for x in run["cost_bps"]]
    selection_cost = max(costs)

    if bool(run.get("sealed_opened")):
        winner = run.get("winner_variant")
        winner_segment = str(run.get("winner_segment") or "all")
        result = dict(run.get("result_json") or {})
        if not winner:
            result.update({"sealed_test_opened": True, "final_after_sealed_verdict": "rejected_no_validation_winner"})
            return None, None, "rejected_no_validation_winner", result
        execute("delete from public.dip_metrics where run_id=%s and split='sealed_test'", (run["id"],))
        selected_metrics: dict[str, Any] | None = None
        sealed = frame[(frame.get("split") == "sealed_test") & (frame.get("variant_key") == winner)].copy() if not frame.empty else pd.DataFrame()
        sealed = _segment_frame(sealed, winner_segment)
        for cost_bps in costs:
            metrics = performance_metrics(sealed, cost_bps, settings.bootstrap_iterations, float(run["target_net_pct"]))
            p_value = float(metrics.get("bootstrap_p_one_sided") if metrics.get("bootstrap_p_one_sided") is not None else 1.0)
            _write_metric(str(run["id"]), "sealed_test", winner_segment, cost_bps, winner, metrics, p_value, p_value)
            if cost_bps == selection_cost:
                selected_metrics = metrics

        pre_verdict = str(result.get("pre_sealed_verdict") or run.get("verdict") or "rejected")
        reasons: list[str] = []
        metrics = selected_metrics or {"observations": 0}
        strong_sealed = (
            _metric_positive(metrics, 30, 10)
            and (metrics.get("mean_net_return_pct") or 0) >= 0.5
            and (metrics.get("net_target_success_rate_pct") or 0) >= 50.0
            and (metrics.get("net_target_wilson_low_pct") or 0) >= 35.0
            and (metrics.get("net_target_daily_ci_low_pct") or 0) >= 30.0
            and (metrics.get("loss_5pct_rate_pct") if metrics.get("loss_5pct_rate_pct") is not None else 100) <= 30.0
            and (metrics.get("bootstrap_ci_low_pct") if metrics.get("bootstrap_ci_low_pct") is not None else -999) > 0
            and (metrics.get("profit_factor") or 0) >= 1.2
            and (metrics.get("best_symbol_profit_share") is None or metrics.get("best_symbol_profit_share") <= 0.25)
        )
        positive_sealed = (
            _metric_positive(metrics, 20, 5)
            and (metrics.get("net_target_success_rate_pct") or 0) >= 35.0
        )
        if pre_verdict == "validation_passed_pending_sealed_test" and strong_sealed:
            final_verdict = "validated_for_paper_testing"
        elif pre_verdict in {"validation_passed_pending_sealed_test", "promising_but_unproven_pending_sealed_test"} and positive_sealed:
            final_verdict = "promising_but_unproven"
            reasons.append("sealed result is positive but the combined validation/sealed evidence does not meet every strict paper-test gate")
        elif metrics.get("observations", 0) == 0:
            final_verdict = "sealed_test_unavailable"
            reasons.append("the validation-selected entry was not executable in the sealed split")
        else:
            final_verdict = "rejected"
            reasons.append("the validation-selected entry failed to retain sufficiently robust positive net performance in the sealed split")
        result.update(
            {
                "sealed_test_opened": True,
                "sealed_test_processed": True,
                "final_after_sealed_verdict": final_verdict,
                "sealed_reasons": reasons,
            }
        )
        return str(winner), winner_segment, final_verdict, result

    execute("delete from public.dip_metrics where run_id=%s and split in ('discovery','validation')", (run["id"],))
    if frame.empty or frame[frame["split"].isin(["discovery", "validation"])].empty:
        return None, None, "rejected_no_trials", {"reason": "No executable discovery/validation trials were created", "sealed_test_opened": False}

    metric_records: list[dict[str, Any]] = []
    for split in ["discovery", "validation"]:
        for cost_bps in costs:
            group_records: list[dict[str, Any]] = []
            variants = sorted(frame.loc[frame["split"] == split, "variant_key"].unique())
            p_values: list[float] = []
            for segment in SEGMENTS:
                segmented = _segment_frame(frame[frame["split"] == split], segment)
                for variant in variants:
                    subset = segmented[segmented["variant_key"] == variant].copy()
                    if subset.empty:
                        continue
                    metrics = performance_metrics(subset, cost_bps, settings.bootstrap_iterations, float(run["target_net_pct"]))
                    p_value = float(metrics.get("bootstrap_p_one_sided") if metrics.get("bootstrap_p_one_sided") is not None else 1.0)
                    group_records.append({"split": split, "segment_key": segment, "cost_bps": cost_bps, "variant_key": variant, "metrics": metrics, "p_value": p_value})
                    p_values.append(p_value)
            q_values = benjamini_hochberg(p_values)
            for record, q_value in zip(group_records, q_values):
                record["q_value"] = q_value
                metric_records.append(record)
                _write_metric(
                    str(run["id"]), split, record["segment_key"], cost_bps, record["variant_key"],
                    record["metrics"], record["p_value"], q_value,
                )

    validation = [
        record for record in metric_records
        if record["split"] == "validation" and record["cost_bps"] == selection_cost
    ]
    winner, diagnostic_best, tier = select_validation_record(validation)
    verdict = "rejected_no_validation_evidence" if not validation else "rejected"
    winner_key: str | None = winner["variant_key"] if winner else None
    winner_segment: str | None = winner["segment_key"] if winner else None
    reasons: list[str] = []
    if tier == "strong" and winner:
        verdict = "validation_passed_pending_sealed_test"
    elif tier == "promising" and winner:
        verdict = "promising_but_unproven_pending_sealed_test"
        metrics = winner["metrics"]
        if not ((metrics.get("bootstrap_ci_low_pct") if metrics.get("bootstrap_ci_low_pct") is not None else -999) > 0):
            reasons.append("validation confidence interval includes zero")
        if (metrics.get("net_target_success_rate_pct") or 0) < 55.0:
            reasons.append("validation probability of achieving the net target is below 55%")
        if (metrics.get("net_target_wilson_low_pct") or 0) < 45.0:
            reasons.append("event-level Wilson lower bound for net-target success is below 45%")
        if (metrics.get("net_target_daily_ci_low_pct") or 0) < 40.0:
            reasons.append("date-resampled lower bound for net-target success is below 40%")
        if float(winner.get("q_value") if winner.get("q_value") is not None else 1.0) > 0.05:
            reasons.append("multiple-testing-adjusted q-value exceeds 0.05")
        if metrics.get("observations", 0) < 100 or metrics.get("independent_dates", 0) < 20:
            reasons.append("sample below strong-evidence threshold")
    elif validation:
        reasons.append("no validation variant met even the minimum promising-evidence gate")

    best_validation_variant = diagnostic_best["variant_key"] if diagnostic_best else None
    best_validation_segment = diagnostic_best["segment_key"] if diagnostic_best else None
    result = {
        "selection_cost_bps": selection_cost,
        "best_validation_variant": best_validation_variant,
        "best_validation_segment": best_validation_segment,
        "winner_variant": winner_key,
        "winner_segment": winner_segment,
        "pre_sealed_verdict": verdict,
        "reasons": reasons,
        "sealed_test_opened": False,
        "sealed_test_processed": False,
        "policy": "A validation-qualified winner is frozen before sealed candidates are processed. A rejected validation result cannot open the sealed test.",
    }
    return winner_key, winner_segment, verdict, result


async def process_run(run: dict[str, Any], alpaca: AlpacaClient) -> None:
    run_id = str(run["id"])
    logger.info("Processing run %s (%s)", run_id, run["name"])
    try:
        _heartbeat(run_id, "loading_market_calendar")
        calendar = await alpaca.calendar(run["start_date"], run["end_date"])
        sessions = _calendar_sessions(calendar)
        if not sessions:
            raise RuntimeError("Alpaca returned no market sessions for the requested date range")

        _heartbeat(run_id, "generating_candidates")
        count_row = fetch_one("select count(*) as n from public.dip_candidates where run_id=%s", (run_id,)) or {"n": 0}
        if int(count_row["n"]) == 0:
            if run["source_mode"] == "scanner_alerts":
                generated = _scanner_alert_candidates(run)
            elif run["source_mode"] == "manual_symbols":
                generated = _manual_symbol_candidates(run, sessions)
            elif run["source_mode"] == "candidate_csv":
                generated = 0
            else:
                raise RuntimeError(f"Unsupported source mode {run['source_mode']}")
            logger.info("Generated %s candidate rows", generated)
        _sync_market_calendar(run, sessions)
        _assign_splits(run)
        _refresh_counts(run_id)

        _heartbeat(run_id, "processing_candidates")
        candidates = fetch_all(
            """
            select * from public.dip_candidates
            where run_id=%s and status in ('queued','failed')
            order by trade_date, symbol
            """,
            (run_id,),
        )
        for index, candidate in enumerate(candidates, start=1):
            if _cancel_requested(run_id):
                execute(
                    "update public.dip_runs set status='cancelled', stage='cancelled', completed_at=now(), heartbeat_at=now() where id=%s",
                    (run_id,),
                )
                _update_runtime("idle")
                return
            _heartbeat(run_id, f"processing_candidates_{index}_of_{len(candidates)}")
            await _process_candidate(run, candidate, alpaca)
            if index % 10 == 0 or index == len(candidates):
                _refresh_counts(run_id)
                _heartbeat(run_id, f"processing_candidates_{index}_of_{len(candidates)}")

        _heartbeat(run_id, "computing_metrics")
        winner, winner_segment, verdict, result = _compute_metrics(run)
        _refresh_counts(run_id)
        failed = fetch_one("select count(*) as n from public.dip_candidates where run_id=%s and status='failed'", (run_id,)) or {"n": 0}
        final_status = "completed_with_warnings" if int(failed["n"]) > 0 else "completed"
        final_stage = "completed_sealed_test" if run.get("sealed_opened") else ("awaiting_sealed_test" if winner else "completed_no_validation_winner")
        execute(
            """
            update public.dip_runs
            set status=%s, stage=%s, winner_variant=%s, winner_segment=%s, verdict=%s, result_json=%s,
                completed_at=now(), heartbeat_at=now(), last_error=null
            where id=%s
            """,
            (final_status, final_stage, winner, winner_segment, verdict, Jsonb(result), run_id),
        )
        logger.info("Completed run %s: %s winner=%s", run_id, verdict, winner)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Run failed: %s", run_id)
        execute(
            """
            update public.dip_runs
            set status='failed', stage='failed', retry_count=retry_count+1,
                last_error=%s, heartbeat_at=now()
            where id=%s
            """,
            (message[:4000], run_id),
        )
        _issue(run_id, "process_run", message + "\n" + traceback.format_exc(), "error")
        _update_runtime("error", run_id, message)
        return
    _update_runtime("idle")


async def worker_loop() -> None:
    settings.validate_worker()
    alpaca = AlpacaClient()
    _update_runtime("idle")
    logger.info("Buy-the-dip worker started; feed=%s", settings.alpaca_feed)
    try:
        while True:
            _recover_stale_work()
            claimed = fetch_one("select * from public.claim_next_dip_run()")
            if claimed:
                await process_run(claimed, alpaca)
            else:
                _update_runtime("idle")
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await alpaca.close()


if __name__ == "__main__":
    asyncio.run(worker_loop())
