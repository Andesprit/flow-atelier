"""Real Git branches and shell measurements, with fake ACP and GitHub boundaries."""
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase, child_environ, drop_credential_env
from flow_atelier.services.executor.bash import to_bash_path


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class Agent(ExecutorBase):
    def __init__(self):
        self.builds = 0
        self.value = "70"
        self.prompts = []
        self.inherited_publisher_env = []
        self.vote = "A"

    async def execute(self, task, resolved_command, context):
        self.inherited_publisher_env.append({"GH_TOKEN", "SSH_AUTH_SOCK"} & child_environ().keys())
        self.prompts.append((task.name, resolved_command))
        if task.name == "propose":
            assert task.permissions == "read-only"
            assert "Prior observation" in resolved_command
            output = json.dumps({"key": "cache-lookup", "hypothesis": "Cache the lookup"})
        elif task.name.startswith("judge_"):
            assert task.permissions == "read-only"
            assert not list(context.working_dir.iterdir())
            output = self.vote
        else:
            assert task.name == "build" and task.permissions == "edits"
            self.builds += 1
            (context.working_dir / "value").write_text(self.value)
            output = "Changed the lookup"
        return ExecutionResult(output=output)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    for key, value in {"GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                       "GIT_AUTHOR_EMAIL": "fixture@example.test",
                       "GIT_COMMITTER_EMAIL": "fixture@example.test"}.items():
        monkeypatch.setenv(key, value)
    config = project / "docs/improvement"
    config.mkdir(parents=True)
    (config / "latency.sh").write_text('cat value\n')
    (config / "test.sh").write_text('test "$(cat value)" != 50\n')
    (project / "docs/improvement-log.md").write_text("# Decisions\nPrior observation\n")
    (project / "value").write_text("100")
    git(project, "add", ".")
    git(project, "commit", "-m", "Baseline")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(project), str(remote)], check=True)
    git(project, "remote", "add", "origin", str(remote))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\ncase "$1 $2" in\n'
                  '"pr list") echo \'[{"headRefName":"atelier/improvements/open","title":"Other idea","url":"test"}]\';;\n'
                  '"pr create") [ ! -f "$GH_RECORD.block" ] || exit 2; '
                  'echo "$@" >> "$GH_RECORD"; echo "https://example.test/pr/1";;\n'
                  '*) exit 21;;\nesac\n')
    gh.chmod(0o755)
    record = tmp_path / "publication"
    monkeypatch.setenv("GH_RECORD", str(record))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    atelier = Atelier(base_dir=tmp_path / "launcher/.atelier")
    agent = Agent()
    atelier.executors["harness:codex"] = agent
    atelier.executors["harness:claude-code"] = agent
    return project, remote, record, atelier, agent


def install_drivers(project, files):
    for name, content in files.items():
        (project / "docs/improvement" / name).write_text(content)
    git(project, "add", "docs/improvement")
    git(project, "commit", "-m", "Define fixed checks")
    git(project, "push", "origin", "main")


def active(atelier, project):
    return atelier.store.create_flow("continuous-improvement", {}, project=project)


@pytest.mark.parametrize(("new", "accepted"), [("70", True), ("99", False), ("120", False), ("50", False)])
async def test_latency_pr_contains_only_the_earned_changes(repo, new, accepted):
    from flow_atelier.modules.improvement import experiment, propose

    project, remote, record, atelier, agent = repo
    run = active(atelier, project)
    agent.value = new
    proposal = await propose(atelier, run, project, "latency")
    assert "Other idea" in agent.prompts[0][1]
    result = await experiment(atelier, run, project, "latency", "nightly", proposal)
    assert result["accepted"] is accepted
    assert result["baseline"] == [100] * 5 and result["candidate"] == [int(new)] * 5
    assert " --base main " in " " + record.read_text()
    branch = result["branch"]
    assert git(remote, "show", branch + ":value") == (new if accepted else "100")
    decision = git(remote, "show", branch + ":docs/improvement-log.md")
    assert "cache-lookup" in decision and result["reason"] in decision and "nightly" in decision
    assert git(project, "status", "--porcelain") == ""
    assert git(remote, "show", "main:value") == "100"


async def test_concurrent_rejection_publishes_one_log_pr_and_merge_rules_it_out(repo):
    from flow_atelier.modules.improvement import experiment, propose

    project, remote, record, atelier, agent = repo
    first, second = [active(atelier, project) for _ in range(2)]
    agent.value = "110"
    proposal = await propose(atelier, first, project, "latency")
    results = await asyncio.gather(*(
        experiment(atelier, run, project, "latency", "manual", proposal)
        for run in [first, second]
    ))
    winner = next(row for row in results if "accepted" in row)
    assert agent.builds == 1 and len(record.read_text().splitlines()) == 1
    git(remote, "update-ref", "refs/heads/main", git(remote, "rev-parse", winner["branch"]))
    git(remote, "update-ref", "-d", "refs/heads/" + winner["branch"])
    third = active(atelier, project)
    result = await experiment(atelier, third, project, "latency", "manual", proposal)
    assert "decision log" in result["reason"] and agent.builds == 1


def test_noise_exceeds_observed_gain():
    from flow_atelier.modules.improvement import compare_samples

    assert not compare_samples([80, 120, 100, 119, 81], [72, 108, 90, 107, 73], 5)[0]
    assert not compare_samples([80, 120, 100, 119, 81], [80, 120, 100, 119, 81], 5)[0]


async def test_retry_after_push_opens_pr_without_rebuilding(repo):
    from flow_atelier.modules.improvement import experiment, propose

    project, _, record, atelier, agent = repo
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "latency")
    blocked = record.with_suffix(".block")
    blocked.touch()
    with pytest.raises(ValueError, match="gh pr exited"):
        await experiment(atelier, run, project, "latency", "manual", proposal)
    blocked.unlink()
    result = await experiment(atelier, run, project, "latency", "manual", proposal)
    assert result["reason"] == "recovered published branch"
    assert agent.builds == 1 and len(record.read_text().splitlines()) == 1


async def test_cost_from_fixed_meter_and_rates(repo):
    from flow_atelier.modules.improvement import experiment, propose

    project, _, _, atelier, _ = repo
    install_drivers(project, {
        "cost.sh": """printf '{"input_tokens":%s,"output_tokens":10}' "$(cat value)" """,
        "settings.json": json.dumps({"cost": {"input_per_million": 2, "output_per_million": 4}}),
    })
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "cost")
    result = await experiment(atelier, run, project, "cost", "weekly", proposal)
    assert result["accepted"] and result["unit"] == "USD"
    assert result["baseline"] == [0.00024] * 5
    assert result["candidate"] == [0.00018] * 5


@pytest.mark.parametrize("report", ["NaN", "-1", '{"input_tokens":true,"output_tokens":2}'])
def test_invalid_cost_evidence_is_rejected(report):
    from flow_atelier.modules.improvement_metrics import MetricsSettings, numeric_sample

    with pytest.raises(ValueError):
        numeric_sample("cost", report, MetricsSettings(unit="USD"))


async def test_installed_recipe_runs_through_shell_cli_and_acp(repo, tmp_path, monkeypatch):
    project, remote, _, _, _ = repo
    fixture = Path(__file__).resolve().parents[1] / "fixtures/fake_acp_agent.py"
    cli = tmp_path / "bin/atelier"
    cli.write_text(f'#!/bin/sh\nexec {shlex.quote(to_bash_path(sys.executable))} -m flow_atelier.main "$@"\n')
    cli.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", str(fixture.parents[2]))
    for name, mode, turn in [
        ("CODEX", "read-only", {"chunks": ['{"key":"cli","hypothesis":"Cache work"}']}),
        ("CLAUDE", "acceptEdits", {"write": {"path": "value", "text": "75"}, "chunks": ["done"]}),
    ]:
        script = {"modes": {"current": mode, "available": [{"id": mode, "name": mode}]},
                  "turns": [turn]}
        monkeypatch.setenv(f"ATELIER_{name}_LAUNCH_CMD", json.dumps([
            sys.executable, str(fixture), "--script", json.dumps(script),
        ]))
    launcher = Atelier(base_dir=project / ".atelier")
    launcher.install_package("builtin:platform")
    run = await launcher.run_conduit("continuous-improvement", {"project": str(project)},
                                     working_dir=project)
    measured = json.loads(launcher.get_outputs(run)["measure"])
    assert measured["accepted"] and git(remote, "show", measured["branch"] + ":value") == "75"


async def test_measurement_cannot_replace_the_source_that_was_tested(repo):
    from flow_atelier.modules.improvement import experiment, propose

    project, remote, _, atelier, _ = repo
    install_drivers(project, {"latency.sh": 'cat value\nif [ "$(cat value)" = 70 ]; then echo 60 >value; fi\n'})
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "latency")
    decision = await experiment(atelier, run, project, "latency", "nightly", proposal)
    assert not decision["accepted"] and "changed" in decision["reason"]
    assert git(remote, "show", decision["branch"] + ":value") == "100"


async def test_workers_do_not_inherit_publisher_environment(repo, monkeypatch):
    from flow_atelier.modules.improvement import experiment, propose

    project, _, _, atelier, agent = repo
    monkeypatch.setenv("GH_TOKEN", "fixture-only")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/no/agent")
    install_drivers(project, {"test.sh": 'test -z "${GH_TOKEN-}${SSH_AUTH_SOCK-}"\n'})
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "latency")
    result = await experiment(atelier, run, project, "latency", "nightly", proposal)
    assert result["accepted"] and os.environ["GH_TOKEN"] == "fixture-only"
    assert agent.inherited_publisher_env == [set(), set()]
    assert child_environ()["GH_TOKEN"] == "fixture-only"


async def test_credential_filter_is_nested_and_local_to_the_current_task(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fixture")
    monkeypatch.setenv("SSH_AUTH_SOCK", "fixture")

    async def sibling():
        await asyncio.sleep(0)
        return child_environ()

    other = asyncio.create_task(sibling())
    with drop_credential_env("GH_TOKEN"):
        with drop_credential_env("SSH_AUTH_SOCK"):
            assert {"GH_TOKEN", "SSH_AUTH_SOCK"}.isdisjoint(child_environ())
        assert child_environ()["SSH_AUTH_SOCK"] == "fixture"
        assert (await other)["GH_TOKEN"] == "fixture"
    assert child_environ()["GH_TOKEN"] == "fixture"


@pytest.mark.parametrize(("flip", "votes", "accept"), [
    (0, ("A", "B"), True), (1, ("A", "B"), False), (0, ("A", "A"), False),
])
async def test_two_blind_votes_are_mapped_to_the_real_versions(repo, monkeypatch, flip, votes, accept):
    from flow_atelier.modules.improvement import experiment, propose

    project, remote, _, atelier, coder = repo
    codex = Agent()
    atelier.executors["harness:codex"] = codex
    codex.vote, coder.vote = votes
    (project / "STYLE.md").write_text("Prefer readable names for measured work.")
    atelier.settings.step_instructions.project_rules[str(project.resolve())] = ["STYLE.md"]
    install_drivers(project, {"clean-code.sh": """echo '{"complexity":2,"function_length":10,
        "duplication":0,"dead_code":0,"lint_warnings":0,"type_warnings":0}'"""})
    monkeypatch.setattr("flow_atelier.modules.improvement.secrets.randbelow", lambda _: flip)
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "clean-code")
    result = await experiment(atelier, run, project, "clean-code", "manual", proposal)
    assert result["accepted"] is accept
    assert git(remote, "show", result["branch"] + ":value") == ("70" if accept else "100")
    for judge in [codex, coder]:
        prompt = next(p for name, p in judge.prompts if name.startswith("judge_"))
        assert "Prefer readable names" in prompt and "Cache the lookup" not in prompt
        assert str(project.resolve()) not in prompt
        assert "baseline" not in prompt.lower() and "candidate" not in prompt.lower()
    assert all("Prefer readable names" in p for actor in [codex, coder]
               for name, p in actor.prompts if name in {"propose", "build"})
    assert set(result["baseline"][0]) == {
        "complexity", "function_length", "duplication", "dead_code", "lint_warnings", "type_warnings",
    }


@pytest.mark.parametrize("regression", ["behavior", "metrics"])
async def test_clean_code_guardrails_reject_without_publishing_code(repo, regression):
    from flow_atelier.modules.improvement import experiment, propose

    project, remote, _, atelier, agent = repo
    agent.value = "50" if regression == "behavior" else "70"
    (project / "STYLE.md").write_text("Keep it simple")
    atelier.settings.step_instructions.project_rules[str(project.resolve())] = ["STYLE.md"]
    install_drivers(project, {"clean-code.sh": """n=0
if [ "$(cat value)" = 70 ]; then n=2; fi
printf '{"complexity":4,"function_length":10,"duplication":0,"dead_code":0,"lint_warnings":0,"type_warnings":%s}' "$n"
"""})
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "clean-code")
    result = await experiment(atelier, run, project, "clean-code", "manual", proposal)
    assert not result["accepted"]
    assert ("Behavior checks" if regression == "behavior" else "type_warnings") in result["reason"]
    assert git(remote, "show", result["branch"] + ":value") == "100"


@pytest.mark.parametrize("keyboard", ["true", "false"])
async def test_ux_requires_keyboard_access_and_fixed_lighthouse_report(repo, tmp_path, keyboard):
    from flow_atelier.modules.improvement import experiment, propose

    project, _, _, atelier, _ = repo
    lighthouse = tmp_path / "bin/lighthouse"
    lighthouse.write_text("#!/bin/sh\necho '{\"categories\":{\"performance\":{\"score\":0.9},\"accessibility\":{\"score\":1.0}}}'\n")
    lighthouse.chmod(0o755)
    install_drivers(project, {"ux.sh": f"""lighthouse > "$1"
steps=4; keyboard=true
if [ "$(cat value)" = 70 ]; then steps=3; keyboard={keyboard}; fi
printf '{{"steps":%s,"keyboard_passed":%s,"task_completed":true,"accessibility_violations":0}}' "$steps" "$keyboard"
"""})
    run = active(atelier, project)
    proposal = await propose(atelier, run, project, "ux")
    result = await experiment(atelier, run, project, "ux", "manual", proposal)
    assert result["accepted"] is (keyboard == "true")
    assert all(sample["steps"] == 3 and sample["lighthouse_accessibility"] == 1.0
               for sample in result["candidate"])
    if keyboard == "false":
        assert "keyboard" in result["reason"]


async def test_python_metrics_without_a_project_adapter(repo, tmp_path):
    from flow_atelier.modules.improvement import propose

    project, _, _, atelier, _ = repo
    (project / "app.py").write_text("def example(x):\n    if x:\n        return x\n    return None\n")
    git(project, "add", "app.py")
    git(project, "commit", "-m", "Python source")
    git(project, "push", "origin", "main")
    reports = {
        "radon": ('{"app.py":[{"complexity":2}]}', 0),
        "ruff": ('[{"code":"F401"}]', 1),
        "pyright": ('{"summary":{"errorCount":2,"warningCount":1}}', 1),
        "vulture": ("app.py:7: unreachable code (100% confidence)", 3),
    }
    for name, (output, code) in reports.items():
        tool = tmp_path / "bin" / name
        tool.write_text(f"#!/bin/sh\necho '{output}'\nexit {code}\n")
        tool.chmod(0o755)
    result = await propose(atelier, active(atelier, project), project, "clean-code")
    assert result["initial"] == {"complexity": 2, "function_length": 4, "duplication": 0,
                                 "dead_code": 1, "lint_warnings": 1, "type_warnings": 3}

