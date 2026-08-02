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
