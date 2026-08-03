from pathlib import Path


def test_stale_recovery_sql_qualifies_ambiguous_columns():
    source = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")

    assert "update public.dip_candidates as c" in source
    assert "retry_count=c.retry_count+1" in source
    assert "coalesce(c.last_error || '; ', '')" in source
    assert "update public.dip_runs as dr" in source
    assert "retry_count=dr.retry_count+1" in source


def test_scanner_alert_source_supports_original_scanner_audit_columns():
    source = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")

    assert '"first_alerted_at"' in source
    assert "left join public.live_scan_jobs as j on j.id = a.job_id" in source
    assert "j.cutoff_at as availability_job_cutoff_at" in source
    assert '{"created_at", "first_alerted_at"}' in source
    assert "a.decision is distinct from 'reject'" in source


def test_scanner_alert_source_recognises_calibration_job_metadata():
    root = Path(__file__).resolve().parents[1] / "app"
    worker_source = (root / "worker.py").read_text(encoding="utf-8")
    research_source = (root / "research.py").read_text(encoding="utf-8")

    assert "job_marks_historical_calibration" in worker_source
    assert '"calibrat" in job_source' in research_source
    assert '"calibration_request_id"' in research_source
    assert "j.source::text as job_source" in worker_source
    assert "j.parameters as job_parameters" in worker_source
    assert "is_calibration = job_marks_historical_calibration(row)" in worker_source


def test_historical_calibration_metadata_classifier():
    from app.research import job_marks_historical_calibration

    assert job_marks_historical_calibration({"source_decision": "calibration"})
    assert job_marks_historical_calibration({"job_source": "historical_calibration"})
    assert job_marks_historical_calibration({"job_parameters": {"calibration_request_id": "abc"}})
    assert job_marks_historical_calibration({"job_parameters": {"mode": "calibration_bootstrap"}})
    assert not job_marks_historical_calibration({"source_decision": "research", "job_source": "scheduled"})


def test_confirmation_jsonb_parameters_have_explicit_postgres_types():
    root = Path(__file__).resolve().parents[1] / "app"
    worker_source = (root / "worker.py").read_text(encoding="utf-8")
    main_source = (root / "main.py").read_text(encoding="utf-8")

    assert "'confirmation_target_sessions',%s::int" in worker_source
    assert "'completed_sessions_available',%s::int" in worker_source
    assert "'historical_sessions_available',%s::int" in worker_source
    assert "'confirmation_start_date',%s::text" in worker_source
    assert "'confirmation_end_date',%s::text" in worker_source
    assert "'confirmation_mode',%s::text" in worker_source
    assert "'confirmation_target_sessions',%s::int" in main_source
