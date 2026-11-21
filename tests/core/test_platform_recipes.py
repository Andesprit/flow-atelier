"""Install the shipped recipes and check what each selected harness receives."""
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.core.step_instructions import StepInstructions

NAMES = {"error-to-pr", "idea-to-product", "continuous-improvement", "meetings-to-tasks"}
FAKE = Path(__file__).resolve().parents[1] / "fixtures/fake_acp_agent.py"


def test_install_platform_in_two_repos(tmp_path, monkeypatch, _isolate_global_atelier_dir):
    home = _isolate_global_atelier_dir
    repos = [tmp_path / "one", tmp_path / "two"]
    for repo in repos:
        repo.mkdir()
    monkeypatch.chdir(repos[0])
    cli = CliRunner()
    args = ["install", "builtin:platform"]
    install = cli.invoke(app, args)
    assert install.exit_code == 0, install.output
    folder = home / "conduits"
    assert {p.name for p in folder.iterdir()} == NAMES
    for repo in repos:
        assert Atelier(base_dir=repo / ".atelier").store.read_conduit("error-to-pr").name == "error-to-pr"
    recipe = folder / "error-to-pr/conduit.yaml"
    edited = recipe.read_text() + "\n# user edit\n"
    recipe.write_text(edited)
    assert cli.invoke(app, args).exit_code == 0
    assert recipe.read_text() == edited
    lock = json.loads((home / "installed.json").read_text())["platform"]
    assert lock["version"] == 2 and set(lock["conduits"]) == NAMES
    assert lock["source"] == "builtin:platform"
    assert not list((home / "schedules").glob("*.yaml"))
    local = repos[1] / ".atelier/conduits/error-to-pr"
    local.mkdir(parents=True)
    (local / "conduit.yaml").write_text(edited.replace("description:", "description: local #", 1))
    assert Atelier(base_dir=repos[1] / ".atelier").store.read_conduit("error-to-pr").description == "local"


async def test_both_harnesses_receive_selected_context(tmp_path, monkeypatch, _isolate_global_atelier_dir):
    home = _isolate_global_atelier_dir
    repo = tmp_path / "trusted-project"
    store = repo / ".atelier"
    recipe = store / "conduits/demo"
    recipe.mkdir(parents=True)
    (recipe / "conduit.yaml").write_text(
        "name: demo\ndescription: demo\ntasks:\n" + "".join(
            f"  - {name}:\n      description: read rules\n      task: inspect\n"
            f"      tool: harness:{harness}\n      depends_on: []\n"
            for name, harness in [("worker", "claude-code"), ("reviewer", "codex")]
        )
    )
    (repo / "CLAUDE.md").write_text("Keep the fixture deterministic.")
    (repo / "unused.md").write_text("EXCLUDED")
    skills = Path.home() / ".claude/skills"
    skills.mkdir(parents=True)
    source = tmp_path / "skill-source"
    source.mkdir()
    (source / "SKILL.md").write_text("Use the linked test skill.")
    (skills / "review").symlink_to(source, target_is_directory=True)
    config = {"project_rules": {str(repo.resolve()): ["CLAUDE.md"]}, "skills": ["review"]}
    (home / "settings.json").write_text(json.dumps({"stepInstructions": config}))
    (store / "settings.json").write_text(json.dumps({"stepInstructions": {"skills": ["injected"]}}))
    records = []
    for name in ("CLAUDE", "CODEX"):
        record = tmp_path / name
        records.append(record)
        script = {"record_path": str(record), "turns": [{"chunks": ["done"]}]}
        argv = [sys.executable, str(FAKE), "--script", json.dumps(script)]
        monkeypatch.setenv(f"ATELIER_{name}_LAUNCH_CMD", json.dumps(argv))
    await Atelier(base_dir=store).run_conduit("demo", {}, working_dir=repo)
    prompts = [json.loads(record.read_text())[0]["text"] for record in records]
    assert prompts[0] == prompts[1]
    assert "Keep the fixture deterministic." in prompts[0]
    assert "Use the linked test skill." in prompts[0]
    assert "EXCLUDED" not in prompts[0]
    config["skills"].append("unreadable")
    (home / "settings.json").write_text(json.dumps({"stepInstructions": config}))
    with pytest.raises(ValueError, match="unreadable"):
        await Atelier(base_dir=store).run_conduit("demo", {}, working_dir=repo)


def test_rule_symlink_cannot_leave_trusted_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "private.md"
    target.write_text("private")
    (repo / "rules.md").symlink_to(target)
    selected = StepInstructions(project_rules={str(repo.resolve()): ["rules.md"]})
    with pytest.raises(ValueError, match="outside the target project"):
        selected.render(repo)

