from pathlib import Path


def test_stale_recovery_sql_qualifies_ambiguous_columns():
    source = (Path(__file__).resolve().parents[1] / "app" / "worker.py").read_text(encoding="utf-8")

    assert "update public.dip_candidates as c" in source
    assert "retry_count=c.retry_count+1" in source
    assert "coalesce(c.last_error || '; ', '')" in source
    assert "update public.dip_runs as dr" in source
    assert "retry_count=dr.retry_count+1" in source
