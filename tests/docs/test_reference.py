import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATED_PAGE = "---\ntemplate: generated-reference.html\n---\n\n"


def test_generate_reference(tmp_path):
    output = tmp_path / "reference"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_reference.py"), str(output)],
        cwd=tmp_path,
        check=True,
    )

    cli = (output / "cli.md").read_text(encoding="utf-8")
    assert cli.startswith(GENERATED_PAGE + "# CLI reference\n")
    public_cli, aliases = cli.split("\n## Aliases and hidden commands\n", maxsplit=1)
    assert "**Hidden command**" not in cli
    for command in (
        "run",
        "list conduits",
        "schedule daemon",
        "errors reopen",
        "migrate runs",
        "harness check",
        "runner status",
    ):
        assert f" `atelier {command}`\n" in public_cli
    for alias, command in (
        ("add", "install"),
        ("remove", "uninstall"),
        ("schedule list", "list schedules"),
        ("schedule remove", "schedule rm"),
        ("harness list", "list harnesses"),
        ("scheduler start", "schedule daemon"),
        ("scheduler status", "list schedules"),
    ):
        assert f"\n- `atelier {alias}` — same as `atelier {command}`\n" in aliases
        assert f" `atelier {alias}`\n" not in public_cli
    assert "`atelier experiment`" not in cli
    assert "--install-completion" in cli
    assert r"\[default: 8000\]" in cli
    assert "<span" not in cli

    http = (output / "http-api.md").read_text(encoding="utf-8")
    schema = json.loads((output / "openapi.json").read_text(encoding="utf-8"))
    assert http.startswith(GENERATED_PAGE + "# HTTP API reference\n")
    assert "Authorization: Bearer <token>" in http
    for method, path in (
        ("get", "/conduits"),
        ("delete", "/conduits/{name}"),
        ("get", "/flows/{flow_id}/summary"),
        ("post", "/schedules"),
        ("post", "/tasks/run"),
    ):
        assert f"### `{method.upper()} {path}`" in http
        assert method in schema["paths"][path]
    delete_responses = schema["paths"]["/conduits/{name}"]["delete"]["responses"]
    assert "204" in delete_responses
    assert "content" not in delete_responses["204"]
    assert "200" not in delete_responses
    for path in ("/{full_path}", "/favicon.svg", "/session", "/chat", "/mobile/runs", "/push/key"):
        assert path not in schema["paths"]
    assert "RunTaskInput" in schema["components"]["schemas"]
    assert "MobileLaunch" not in schema["components"]["schemas"]
