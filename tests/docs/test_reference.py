import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generate_reference(tmp_path):
    output = tmp_path / "reference"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_reference.py"), str(output)],
        cwd=tmp_path,
        check=True,
    )

    cli = (output / "cli.md").read_text(encoding="utf-8")
    assert cli.startswith("# CLI reference\n")
    for command in (
        "run",
        "list conduits",
        "schedule daemon",
        "errors reopen",
        "migrate runs",
        "harness check",
        "runner status",
        "scheduler start",
    ):
        assert f" `atelier {command}`\n" in cli
    assert "--install-completion" in cli
    assert r"\[default: 8000\]" in cli
    assert "<span" not in cli

    http = (output / "http-api.md").read_text(encoding="utf-8")
    schema = json.loads((output / "openapi.json").read_text(encoding="utf-8"))
    assert http.startswith("# HTTP API reference\n")
    assert "Authorization: Bearer <token>" in http
    for method, path in (
        ("get", "/conduits"),
        ("get", "/flows/{flow_id}/summary"),
        ("post", "/schedules"),
        ("post", "/tasks/run"),
    ):
        assert f"### `{method.upper()} {path}`" in http
        assert method in schema["paths"][path]
    for path in ("/{full_path}", "/favicon.svg", "/session", "/chat", "/mobile/runs", "/push/key"):
        assert path not in schema["paths"]
    assert "RunTaskInput" in schema["components"]["schemas"]
    assert "MobileLaunch" not in schema["components"]["schemas"]
