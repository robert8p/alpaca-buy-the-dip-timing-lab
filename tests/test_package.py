from pathlib import Path


def test_worker_recovery_sql_qualifies_retry_count():
    text = (Path(__file__).parents[1] / "app" / "worker.py").read_text()
    assert "retry_count=c.retry_count+1" in text
    assert "retry_count=r.retry_count+1" in text


def test_schema_contains_isolated_v2_tables():
    text = (Path(__file__).parents[1] / "supabase" / "schema.sql").read_text()
    for name in ("dip_trigger_runs", "dip_trigger_candidates", "dip_trigger_trials", "dip_trigger_metrics", "dip_trigger_runtime"):
        assert name in text


def test_app_has_no_order_endpoints():
    root = Path(__file__).parents[1] / "app"
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    assert "/v2/orders" not in text
    assert "submit_order" not in text


def test_web_container_starts_uvicorn_without_shell_script_dependency():
    text = (Path(__file__).parents[1] / "Dockerfile").read_text()
    assert 'CMD ["python", "-m", "uvicorn"' in text
    assert 'CMD ["/app/scripts/start_web.sh"]' not in text
