from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import __version__
from .db import fetch_all, fetch_one


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8")


def build_run_export(run_id: str) -> tuple[str, bytes]:
    run = fetch_one("select * from public.dip_runs where id=%s", (run_id,))
    if not run:
        raise ValueError("Run not found")
    candidates = fetch_all(
        """
        select id, run_id, symbol, trade_date, cutoff_at, source, source_alert_id,
               source_available_at, session_open_at, session_close_at, split,
               candidate_features, passed_dip_filter, filter_reasons, status, bar_count,
               quality_flags, retry_count, last_error
        from public.dip_candidates
        where run_id=%s and (%s or split is null or split <> 'sealed_test')
        order by trade_date, symbol
        """,
        (run_id, run["sealed_opened"]),
    )
    trials = fetch_all(
        """
        select candidate_id, symbol, trade_date, split, variant_key, entry_at, entry_price,
               entry_reason, target_price, stop_price, target_hit, first_target_hit_at,
               stop_hit, first_stop_hit_at, exit_at, exit_price, exit_reason,
               gross_return_pct, max_gain_pct, max_drawdown_pct, same_bar_ambiguous
        from public.dip_trials
        where run_id=%s and (%s or split <> 'sealed_test')
        order by trade_date, symbol, variant_key
        """,
        (run_id, run["sealed_opened"]),
    )
    metrics = fetch_all(
        """
        select split, segment_key, variant_key, cost_bps, observations, independent_dates, symbols,
               mean_net_return_pct, median_net_return_pct, win_rate_pct,
               net_target_success_rate_pct, net_target_wilson_low_pct, net_target_daily_ci_low_pct, loss_5pct_rate_pct,
               target_before_stop_rate_pct, stop_before_target_rate_pct,
               bootstrap_ci_low_pct, bootstrap_ci_high_pct, p_value, q_value, metrics_json
        from public.dip_metrics
        where run_id=%s and (%s or split <> 'sealed_test')
        order by split, cost_bps, variant_key
        """,
        (run_id, run["sealed_opened"]),
    )
    issues = fetch_all(
        """
        select i.candidate_id, i.symbol, i.trade_date, i.stage, i.severity, i.message, i.created_at
        from public.dip_issues i
        left join public.dip_candidates c on c.id=i.candidate_id
        where i.run_id=%s and (%s or c.split is null or c.split <> 'sealed_test')
        order by i.created_at
        """,
        (run_id, run["sealed_opened"]),
    )
    payloads = {
        "candidates.csv": _csv_bytes(candidates),
        "trials.csv": _csv_bytes(trials),
        "metrics.csv": _csv_bytes(metrics),
        "issues.csv": _csv_bytes(issues),
        "run.json": json.dumps(run, default=str, sort_keys=True, indent=2).encode("utf-8"),
        "README.txt": (
            "Alpaca Buy-the-Dip Timing Lab export\n"
            "=====================================\n"
            "Fixed-time entries require the exact requested one-minute bar; missing minutes are never forward-filled.\n"
            "Confirmation entries use the next consecutive one-minute bar open.\n"
            "If target and stop occur in the same minute bar, the stop is assumed first.\n"
            "Costs are deducted from gross returns in the metrics files.\n"
            + (
                "Sealed-test outcomes are included because the sealed test was explicitly opened.\n"
                if run["sealed_opened"]
                else "Sealed-test candidates and outcomes are intentionally excluded from this export.\n"
            )
            + "The sealed-test split must not be used to retune the entry variants.\n"
            + "This package is research output, not a trading instruction.\n"
        ).encode("utf-8"),
    }
    manifest = {
        "app_version": __version__,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {"name": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            for name, content in payloads.items()
        ],
    }
    payloads["manifest.json"] = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in payloads.items():
            archive.writestr(name, content)
    filename = f"dip_timing_run_{run_id}.zip"
    return filename, output.getvalue()
