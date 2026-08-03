from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, time, timezone
from decimal import Decimal
from uuid import uuid4
import sys
import types

# Keep this unit test independent of the PostgreSQL driver installed in the
# Docker image; exports.py only needs these two callables at import time.
db_stub = types.ModuleType("app.db")
db_stub.fetch_all = lambda *_args, **_kwargs: []
db_stub.fetch_one = lambda *_args, **_kwargs: None
sys.modules.setdefault("app.db", db_stub)

from app import exports


def test_export_serializes_native_postgres_uuid_time_and_decimal(monkeypatch):
    run_id = str(uuid4())
    run = {
        "id": uuid4(),
        "name": "Export serialization proof",
        "start_date": date(2026, 7, 6),
        "end_date": date(2026, 7, 27),
        "search_start_et": time(9, 45),
        "search_end_et": time(15, 15),
        "sealed_opened": False,
        "target_net_pct": Decimal("3.0"),
        "created_at": datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc),
        "symbols": ["AAPL"],
    }
    monkeypatch.setattr(exports, "fetch_one", lambda *_args, **_kwargs: run)
    result_sets = iter([
        [{"id": uuid4(), "trade_date": date(2026, 7, 6), "source_details": {"rank": 1}}],
        [{"id": 1, "entry_at": datetime(2026, 7, 6, 16, 0, tzinfo=timezone.utc)}],
        [{"id": 1, "mean_net_return_pct": Decimal("1.25"), "metrics_json": {"ok": True}}],
        [{"id": 1, "created_at": datetime(2026, 8, 3, 18, 1, tzinfo=timezone.utc)}],
    ])
    monkeypatch.setattr(exports, "fetch_all", lambda *_args, **_kwargs: next(result_sets))

    filename, payload = exports.build_run_export(run_id)

    assert filename == f"dip_trigger_run_{run_id}.zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        exported_run = json.loads(archive.read("run.json"))
        manifest = json.loads(archive.read("manifest.json"))
        assert exported_run["id"] == str(run["id"])
        assert exported_run["search_start_et"] == "09:45:00"
        assert exported_run["target_net_pct"] == 3.0
        assert manifest["counts"] == {"candidates": 1, "trials": 1, "metrics": 1, "issues": 1}
        assert "candidates.csv" in archive.namelist()
