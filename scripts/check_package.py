from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    required = [
        "Dockerfile", "render.yaml", "requirements.txt", "README.md", "DEPLOYMENT.md",
        "MODEL_SPEC.md", "TROUBLESHOOTING.md", "app/main.py", "app/worker.py",
        "app/research.py", "supabase/schema.sql", "scripts/start_web.sh",
    ]
    missing = [name for name in required if not (ROOT / name).exists()]
    if missing:
        raise SystemExit("Missing required files: " + ", ".join(missing))

    for path in sorted((ROOT / "app").glob("*.py")):
        ast.parse(path.read_text(), filename=str(path))
    for path in sorted((ROOT / "app/templates").glob("*.html")):
        Environment().parse(path.read_text())

    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
    services = blueprint.get("services") or []
    assert len(services) == 2
    assert {service["type"] for service in services} == {"web", "worker"}

    schema = (ROOT / "supabase/schema.sql").read_text()
    for table in ("dip_trigger_runs", "dip_trigger_candidates", "dip_trigger_trials", "dip_trigger_metrics", "dip_trigger_runtime"):
        assert table in schema
    assert "claim_next_dip_trigger_run" in schema

    source = "\n".join(path.read_text() for path in (ROOT / "app").glob("*.py"))
    assert "/v2/orders" not in source
    assert "submit_order" not in source
    assert "retry_count=c.retry_count+1" in source
    assert "retry_count=r.retry_count+1" in source

    result = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, text=True, capture_output=True)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        return result.returncode

    print(json.dumps({"status": "ok", "version": "2.0.2", "services": len(services), "templates": len(list((ROOT / 'app/templates').glob('*.html')))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
