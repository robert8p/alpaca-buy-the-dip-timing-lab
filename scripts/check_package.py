from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "Dockerfile",
    "render.yaml",
    "requirements.txt",
    "README.md",
    "DEPLOYMENT.md",
    "MODEL_SPEC.md",
    "TROUBLESHOOTING.md",
    "app/main.py",
    "app/worker.py",
    "app/research.py",
    "supabase/schema.sql",
    "tests/test_research.py",
]

missing = [path for path in REQUIRED if not (ROOT / path).exists()]
if missing:
    raise SystemExit("Missing required files: " + ", ".join(missing))

files = []
for path in sorted(ROOT.rglob("*")):
    if (
        not path.is_file()
        or ".pytest_cache" in path.parts
        or "__pycache__" in path.parts
        or path.name == "PACKAGE_MANIFEST.json"
    ):
        continue
    relative = path.relative_to(ROOT).as_posix()
    content = path.read_bytes()
    files.append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})

manifest = {"package": "alpaca-buy-the-dip-timing-lab", "version": "1.0.2", "files": files}
(ROOT / "PACKAGE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
print(f"Validated {len(files)} files")
