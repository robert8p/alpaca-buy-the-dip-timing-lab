from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .db import fetch_all, fetch_one


def _json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO()
    if not rows:
        output.write("")
        return output.getvalue().encode()
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        cleaned = {}
        for key, value in row.items():
            cleaned[key] = json.dumps(value, default=_json_default, sort_keys=True) if isinstance(value, (dict, list)) else value
        writer.writerow(cleaned)
    return output.getvalue().encode()


def build_run_export(run_id: str) -> tuple[str, bytes]:
    run = fetch_one("select * from public.dip_trigger_runs where id=%s", (run_id,))
    if not run:
        raise ValueError("Run not found")
    include_sealed = bool(run["sealed_opened"])
    candidates = fetch_all(
        """
        select * from public.dip_trigger_candidates
        where run_id=%s and (%s or split <> 'sealed_test')
        order by trade_date,symbol
        """,
        (run_id, include_sealed),
    )
    trials = fetch_all(
        """
        select * from public.dip_trigger_trials
        where run_id=%s and (%s or split <> 'sealed_test')
        order by trade_date,symbol,recipe_key
        """,
        (run_id, include_sealed),
    )
    metrics = fetch_all(
        """
        select * from public.dip_trigger_metrics
        where run_id=%s and (%s or split <> 'sealed_test')
        order by split,cost_bps desc,segment_key,recipe_key
        """,
        (run_id, include_sealed),
    )
    issues = fetch_all("select * from public.dip_trigger_issues where run_id=%s order by created_at", (run_id,))
    manifest = {
        "app": "Alpaca Dip-Reversal Trigger Discovery Lab",
        "run_id": run_id,
        "sealed_included": include_sealed,
        "counts": {"candidates": len(candidates), "trials": len(trials), "metrics": len(metrics), "issues": len(issues)},
        "warning": "A positive validation result is not live-trading approval. The app contains no order endpoints.",
    }
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, default=_json_default))
        archive.writestr("run.json", json.dumps(run, indent=2, default=_json_default))
        archive.writestr("candidates.csv", _csv_bytes(candidates))
        archive.writestr("trials.csv", _csv_bytes(trials))
        archive.writestr("metrics.csv", _csv_bytes(metrics))
        archive.writestr("issues.csv", _csv_bytes(issues))
    return f"dip_trigger_run_{run_id}.zip", payload.getvalue()
