import asyncio
import json
import shutil
import subprocess
import sys

import pytest

from flow_atelier.schemas.api import CreateScheduleInput
from flow_atelier.services.api.chat_actions import ChatActions
from flow_atelier.services.scheduler import ScheduleStore


def request(op, **args):
    return json.dumps({"atelier": {"op": op, **args}})


@pytest.fixture
def actions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return ChatActions(repo, tmp_path / "global")


async def test_allowlist_and_confirm(actions):
    await actions.reply(request("init"))
    assert (actions.project / ".atelier/conduits/hello/conduit.yaml").exists()
    for bad in [
        request("exec", argv=["touch", "source"]),
        request("confirm"),
        request("start", conduit="../hello"),
        request("init", folder="/"),
        request("start", conduit="hello", inputs={"bogus": "x"}),
    ]:
        with pytest.raises(ValueError):
            await actions.reply(bad)
    assert not list(actions.global_dir.glob("projects/*/*.jsonl"))
    args = {"conduit": "hello", "inputs": {"name": "old"}}
    await actions.reply(request("start", **args))
    first = actions.pending.copy()
    assert first["folder"] == str(actions.project)
    assert first["path"] == str(actions.project / ".atelier/conduits/hello/conduit.yaml")
    assert first["inputs"] == {"name": "old"}
    project_file = actions.project / ".atelier/conduits/hello/conduit.yaml"
    global_file = actions.global_dir / "conduits/hello/conduit.yaml"
    global_file.parent.mkdir(parents=True)
    project_file.rename(global_file)
    with pytest.raises(ValueError, match="action changed"):
        await actions.confirm(first["id"])
    await actions.reply(request("start", **args))
    assert actions.pending["path"] == str(global_file)
    global_file.rename(project_file)
    args["inputs"]["name"] = "new"
    await actions.reply(request("start", **args))
    with pytest.raises(ValueError):
        await actions.confirm(first["id"])
    assert not list(actions.global_dir.glob("projects/*/*.jsonl"))
    token = actions.pending["id"]
    result = json.loads(await actions.confirm(token))
    assert result["status"] == "completed"
    with pytest.raises(ValueError):
        await actions.confirm(token)
    await actions.reply(request("resume", run=result["flow_id"]))
    assert actions.pending["inputs"] == {"name": "new"}
    assert actions.pending["conduit"] == "hello"
    with pytest.raises(ValueError):
        await actions.confirm(token)
    legacy = ScheduleStore(actions.project / ".atelier")
    unrelated = legacy.create(
        CreateScheduleInput(
            conduit_name="unrelated",
            run_path=str(actions.project),
            schedule={"name": "unrelated", "mode": "interval", "every_minutes": 15},
        )
    )
    await actions.reply(
        request(
            "enable",
            schedule={"name": "weekly", "mode": "recurring", "days": [1], "times": ["09:00"]},
            **args,
        )
    )
    assert actions.pending["schedule"]["days"] == [1]
    assert not list(actions.global_dir.glob("schedules/*.yaml"))
    token = actions.pending["id"]
    out = await asyncio.gather(
        actions.confirm(token), actions.confirm(token), return_exceptions=True
    )
    assert sum(isinstance(x, ValueError) for x in out) == 1, out
    assert len(list(actions.global_dir.glob("schedules/*.yaml"))) == 1
    assert legacy.list() == [unrelated]


@pytest.mark.skipif(
    sys.platform != "darwin" and not (sys.platform == "linux" and shutil.which("bwrap")),
    reason="requires native recipe sandbox",
)
async def test_recipe_checkout(actions, tmp_path):
    outside = tmp_path / "outside"
    recipe = {
        "name": "trial",
        "description": "test",
        "tasks": [
            {"step": {"description": "test", "tool": "tool:bash", "task": f'echo no > "{outside}"'}}
        ],
    }
    with pytest.raises(ValueError):
        await actions.reply(request("recipe", conduit="trial", definition=recipe))
    assert not outside.exists()
    assert not (actions.project / ".atelier/conduits/trial/conduit.yaml").exists()
    recipe["tasks"][0]["step"]["task"] = "echo yes > result.txt"
    await actions.reply(request("recipe", conduit="trial", definition=recipe))
    assert actions.pending["replaces_existing"] is False
    assert actions.pending["path"] == str(actions.project / ".atelier/conduits/trial/conduit.yaml")
    assert actions.pending["op"] == "recipe"
    assert not (actions.project / "result.txt").exists()
    await actions.confirm(actions.pending["id"])
    assert (actions.project / ".atelier/conduits/trial/conduit.yaml").is_file()
    await actions.reply(request("recipe", conduit="trial", definition=recipe))
    assert actions.pending["replaces_existing"] is True
    assert not (actions.project / "result.txt").exists()


async def test_resume_and_stop_require_exact_project_run(actions, tmp_path):
    recipe = {
        "name": "retry",
        "description": "fixture",
        "inputs": {"tag": "fixture input"},
        "tasks": [
            {"step": {"description": "check fixture", "tool": "tool:bash", "task": "test -f ready"}}
        ],
    }
    await actions.call("save-recipe", "retry", data=json.dumps(recipe))
    await actions.reply(request("start", conduit="retry", inputs={"tag": "sample"}))
    with pytest.raises(ValueError):
        await actions.confirm(actions.pending["id"])
    run = json.loads(await actions.reply(request("runs")))[0]["flow_id"]
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = ChatActions(other_dir, actions.global_dir)
    for op in ("resume", "stop"):
        with pytest.raises(ValueError, match="another project"):
            await other.reply(request(op, run=run))
    (actions.project / "ready").touch()
    await actions.reply(request("resume", run=run))
    assert actions.pending["inputs"] == {"tag": "sample"}
    assert json.loads(await actions.reply(request("runs")))[0]["status"] == "failed"
    await actions.reply(request("resume", run=run))
    assert json.loads(await actions.confirm(actions.pending["id"]))["flow_id"] == run
    assert json.loads(await actions.reply(request("runs")))[0]["status"] == "completed"

    recipe.update(name="slow", inputs={})
    recipe["tasks"][0]["step"]["task"] = "sleep 30"
    await actions.call("save-recipe", "slow", data=json.dumps(recipe))
    await actions.reply(request("start", conduit="slow"))
    running = asyncio.create_task(actions.confirm(actions.pending["id"]))
    await asyncio.sleep(0)
    try:
        async with asyncio.timeout(15):
            while True:
                rows = json.loads(await actions.reply(request("runs")))
                found = next(
                    (r for r in rows if r["conduit"] == "slow" and r["status"] == "running"), None
                )
                if found:
                    break
                await asyncio.sleep(0.05)
        await actions.reply(request("stop", run=found["flow_id"]))
        assert actions.pending["run"] == found["flow_id"]
        assert actions.pending["conduit"] == "slow"
        assert actions.pending["status"] == "running"
        assert (
            json.loads(await actions.call("status", found["flow_id"], "--json"))["status"]
            == "running"
        )
        await actions.confirm(actions.pending["id"])
        assert (
            next(
                r
                for r in json.loads(await actions.reply(request("runs")))
                if r["flow_id"] == found["flow_id"]
            )["status"]
            == "stopped"
        )
    finally:
        await asyncio.gather(running, return_exceptions=True)


async def test_linux_trial_exposes_only_the_same_python_runtime_alias(tmp_path, monkeypatch):
    from flow_atelier.services.api import chat_cli

    runtime = tmp_path / "python-versioned"
    (runtime / "bin").mkdir(parents=True)
    alias = tmp_path / "python-stable"
    alias.symlink_to(runtime, target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "base_prefix", str(runtime))
    monkeypatch.setattr(sys, "_base_executable", str(alias / "bin/python3"))

    def inspect_mounts(_, roots):
        assert alias in roots and runtime in roots
        assert tmp_path not in roots
        raise RuntimeError("mounts checked")

    monkeypatch.setattr(chat_cli, "sandbox", inspect_mounts)
    with pytest.raises(RuntimeError, match="mounts checked"):
        await chat_cli.cli(tmp_path, tmp_path, "list", trial=True)
