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


def test_schema_and_web_include_true_forward_30_to_90_protocol():
    root = Path(__file__).parents[1]
    schema = (root / "supabase" / "schema.sql").read_text()
    main = (root / "app" / "main.py").read_text()
    worker = (root / "app" / "worker.py").read_text()
    for field in ("run_mode", "parent_run_id", "forward_target_sessions", "frozen_config_sha256"):
        assert field in schema
    assert '/runs/{parent_run_id}/forward' in main
    assert '/runs/{run_id}/extend-forward' in main
    assert "forward_30_pass_extension_available" in main
    assert "_resolve_calendar_for_run" in worker
    assert "frozen forward configuration integrity check failed" in worker.lower()


def test_forward_export_is_locked_while_sealed_window_is_running():
    text = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    assert "Forward results remain sealed until the complete target window has finished" in text

def test_app_supports_distinct_historical_and_forward_sealed_protocols():
    root = Path(__file__).parents[1]
    main = (root / "app" / "main.py").read_text()
    worker = (root / "app" / "worker.py").read_text()
    schema = (root / "supabase" / "schema.sql").read_text()
    assert '/runs/{parent_run_id}/backtest' in main
    assert '/runs/{parent_run_id}/forward' in main
    assert "historical_sealed" in schema
    assert "backtest_scope" in schema
    assert "backtest_90_pass_for_forward_testing" in worker or "backtest_90_pass_for_forward_testing" in (root / "app" / "research.py").read_text()


def test_historical_and_forward_exports_are_locked_while_processing():
    text = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    assert "Forward results remain sealed until the complete target window has finished" in text
    assert "Historical backtest results remain sealed until the complete target window has finished" in text


def test_ui_version_is_dynamic_and_matches_runtime_version():
    root = Path(__file__).parents[1]
    base = (root / "app" / "templates" / "base.html").read_text()
    main = (root / "app" / "main.py").read_text()
    assert "v{{ app_version }}" in base
    assert 'templates.env.globals["app_version"] = __version__' in main
    assert "v2.1.0" not in base
