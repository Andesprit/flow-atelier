"""Real Git branches and shell measurements, with fake ACP and GitHub boundaries."""
import asyncio
import json
import os
import subprocess

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class Agent(ExecutorBase):
    def __init__(self):
        self.builds = 0
        self.value = "70"
        self.prompts = []

    async def execute(self, task, resolved_command, context):
        self.prompts.append((task.name, resolved_command))
        if task.name == "propose":
            assert task.permissions == "read-only"
            assert "Prior observation" in resolved_command
            output = json.dumps({"key": "cache-lookup", "hypothesis": "Cache the lookup"})
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

