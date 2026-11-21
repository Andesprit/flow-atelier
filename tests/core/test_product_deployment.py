"""A human approves the actual source and destination before any cloud identity is used."""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import ExecutionResult
from flow_atelier.services.executor.base import ExecutorBase


class Agent(ExecutorBase):
    def __init__(self, role):
        self.role = role

    async def execute(self, task, resolved_command, context):
        assert task.deny_cloud_config
        assert "fixture-deploy-token" not in resolved_command
        if task.name == "spec":
            text = (
                "A fixture app.\n## Acceptance criteria\n- App runs.\nTest command: test -f app.py"
            )
        elif self.role == "builder":
            assert "structured" in resolved_command and "staging" in resolved_command
            (context.working_dir / "app.py").write_text("print('approved source')\n")
            text = "implemented"
        else:
            text = "PASS"
        return ExecutionResult(output=text)


@pytest.fixture
def target_config():
    return {
        "provider": "cloud-run",
        "project": "fixture-project",
        "region": "us-central1",
        "service": "demo",
        "deploy_account": "deployer@fixture-project.iam.gserviceaccount.com",
        "sources": ["app.py"],
        "report_url": "https://relay.example.test/hooks/errors/staging",
        "report_secret": "staging-error-key:3",
    }


@pytest.mark.parametrize("answer", ["DEPLOY", "decline", "changed after approval"])
async def test_cloud_target_approval_and_source_binding(
    tmp_path,
    monkeypatch,
    target_config,
    answer,
    _isolate_global_atelier_dir,
):
    from flow_atelier.services.executor import deployment

    target = tmp_path / "project"
    target.mkdir()
    monkeypatch.chdir(target)
    (_isolate_global_atelier_dir / "settings.json").write_text(
        json.dumps(
            {
                "deploymentTargets": {str(target.resolve()): {"staging": target_config}},
            }
        )
    )
    atelier = Atelier()
    atelier.install_package("builtin:platform")
    atelier.executors["harness:codex"] = Agent("reviewer")
    atelier.executors["harness:claude-code"] = Agent("builder")
    monkeypatch.setattr(
        "flow_atelier.services.executor.bash.cloud_sandbox", lambda argv, env, *_: (argv, env)
    )
    calls = []
    fake = tmp_path / "gcloud"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o700)
    monkeypatch.setattr(deployment, "_gcloud", lambda: str(fake))

    async def cloud(argv, **kwargs):
        assert argv[0] == str(fake) and kwargs["cwd"] != target
        if argv[1:3] == ["auth", "print-access-token"]:
            calls.extend(["credential read", "mint"])
            assert "--lifetime=600s" in argv
            assert "--impersonate-service-account=" + target_config["deploy_account"] in argv
            return "fixture-deploy-token"
        calls.append("deploy")
        assert argv[1:4] == ["run", "deploy", "demo"]
        assert "--project=fixture-project" in argv and "--region=us-central1" in argv
        assert kwargs["env"]["CLOUDSDK_AUTH_ACCESS_TOKEN"] == "fixture-deploy-token"
        assert "fixture-deploy-token" not in argv and "CLOUDSDK_AUTH_ACCESS_TOKEN" not in os.environ
        source = Path(
            next(arg.removeprefix("--source=") for arg in argv if arg.startswith("--source="))
        )
        assert (source / "app.py").read_text() == "print('approved source')\n"
        return "fixture-deploy-token"

    monkeypatch.setattr(deployment, "_run_cloud", cloud)
    monkeypatch.delenv("CLOUDSDK_AUTH_ACCESS_TOKEN", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    notify = bindir / "atelier"
    notify.write_text('#!/bin/sh\ntest "$1" = notify && test -z "${CLOUDSDK_AUTH_ACCESS_TOKEN-}"\n')
    notify.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    answers = []

    async def respond(prompt, **_):
        answers.append(prompt)
        assert not calls
        if len(answers) == 1:
            return "APPROVE"
        if answer == "changed after approval":
            (target / "app.py").write_text("other source")
            return "DEPLOY"
        return answer

    monkeypatch.setattr("flow_atelier.cli.rendering.multiline_input.multiline_input", respond)
    inputs = {"idea": "fixture", "deployment_target": "staging", "deploy_decision": "DEPLOY"}
    if answer == "changed after approval":
        with pytest.raises(RuntimeError, match="changed"):
            await atelier.run_conduit("idea-to-product", inputs, working_dir=target)
    else:
        run = await atelier.run_conduit("idea-to-product", inputs, working_dir=target)
        outputs = atelier.get_outputs(run)
        plan = json.loads(outputs["deployment_plan"])
        assert plan["target"] == "staging" and plan["service"] == "demo"
        assert plan["source_sha256"] and plan["sources"] == ["app.py"]
        assert "fixture-deploy-token" not in json.dumps(outputs)
        journal = atelier.store.journal(run)
        assert b"fixture-deploy-token" not in journal.path.read_bytes()
        assert all(
            b"fixture-deploy-token" not in path.read_bytes()
            for path in journal.side.rglob("*")
            if path.is_file()
        )
    assert len(answers) == 2
    assert calls == (["credential read", "mint", "deploy"] if answer == "DEPLOY" else [])


async def test_runtime_warning_and_duplicate_failure_reach_one_error_run(
    tmp_path,
    monkeypatch,
    capsys,
):
    from flow_atelier import product_events
    from flow_atelier.core.settings import AtelierSettings
    from flow_atelier.services.api.error_hooks import create_webhook_app

    settings = AtelierSettings(
        webhook_sources={"staging": {"project": str(tmp_path), "conduit": "error-to-pr"}},
        webhook_secrets={"staging": "fixture-signing-secret"},
    )
    app = create_webhook_app(settings)
    monkeypatch.setenv("ATELIER_PRODUCT_TARGET", "staging")
    monkeypatch.setenv("ATELIER_ERROR_URL", "https://relay.example.test/hooks/errors/staging")
    monkeypatch.setenv("ATELIER_ERROR_KEY", "fixture-signing-secret")
    requests = []
    monkeypatch.setattr(
        product_events, "_post", lambda url, body, headers: requests.append((url, body, headers))
    )
    product_events.emit("WARNING", "cache-cold", "Cache is warming")
    assert not requests
    for _ in range(2):
        product_events.emit("ERROR", "save-failed", "Saving the fixture failed")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["severity"] == "WARNING" and lines[0]["target"] == "staging"
    assert "fixture-signing-secret" not in json.dumps(lines)
    recipe = settings.global_atelier_dir / "conduits/error-to-pr"
    recipe.mkdir(parents=True)
    (recipe / "conduit.yaml").write_text(
        "name: error-to-pr\ndescription: fake repair\ninputs:\n  report_b64: report\ntasks:\n"
        "  - receive:\n      tool: tool:bash\n      description: Record the dispatched run\n"
        '      task: echo "$ATELIER_FLOW_ID" >> received\n'
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            answers = [
                await client.post(url, content=body, headers=headers)
                for url, body, headers in requests
            ]
        async with asyncio.timeout(8):
            while not (tmp_path / "received").exists():
                await asyncio.sleep(0.05)
    assert (tmp_path / "received").read_text().splitlines() == [answers[0].json()["run_id"]]
    assert all(r.status_code == 202 for r in answers)
    assert answers[0].json() == answers[1].json()
    assert answers[0].json()["run_id"].endswith("_error-to-pr")


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="Host has no product sandbox")
async def test_both_real_acp_child_processes_are_denied_cloud_config(tmp_path, monkeypatch):
    from flow_atelier.schemas.conduit import Conduit, TaskDefinition

    root = tmp_path / "app"
    root.mkdir()
    cloud = Path.home() / ".config/gcloud"
    cloud.mkdir(parents=True)
    secret = cloud / "fixture"
    secret.write_text("fixture-only")
    monkeypatch.setenv("CLOUDSDK_AUTH_ACCESS_TOKEN", "fixture-only")
    checker = tmp_path / "agent.sh"
    checker.write_text(
        '#!/bin/sh\nif cat "$1" >/dev/null 2>&1 || [ -n "${CLOUDSDK_AUTH_ACCESS_TOKEN-}" ]; then exit 45; fi\n'
        "if touch ../launcher-file 2>/dev/null; then exit 46; fi\n"
        'echo blocked > "$2"\nshift 2\nexec "$@"\n'
    )
    checker.chmod(0o755)
    fake = Path(__file__).resolve().parents[1] / "fixtures/fake_acp_agent.py"
    tasks = []
    for role, harness, env_name in [
        ("build", "claude-code", "CLAUDE"),
        ("review", "codex", "CODEX"),
    ]:
        mode = "acceptEdits" if role == "build" else "read-only"
        script = {
            "modes": {"current": mode, "available": [{"id": mode, "name": mode}]},
            "turns": [{"chunks": ["complete"]}],
        }
        command = [
            str(checker),
            str(secret),
            str(root / role),
            sys.executable,
            str(fake),
            "--script",
            json.dumps(script),
        ]
        monkeypatch.setenv("ATELIER_" + env_name + "_LAUNCH_CMD", json.dumps(command))
        tasks.append(
            TaskDefinition(
                name=role,
                description=role,
                task=role,
                tool="harness:" + harness,
                deny_cloud_config=True,
                permissions="edits" if role == "build" else "read-only",
            )
        )
    tasks.append(
        TaskDefinition(
            name="test",
            description="Check shell isolation",
            tool="tool:bash",
            deny_cloud_config=True,
            depends_on=["build", "review"],
            task='! cat "$HOME/.config/gcloud/fixture" && '
            'test -z "${CLOUDSDK_AUTH_ACCESS_TOKEN-}" && echo blocked > test',
        )
    )
    atelier = Atelier(base_dir=root / ".atelier")
    recipe = Conduit(name="isolation", description="isolation", tasks=tasks)
    if sys.platform == "linux" and not shutil.which("bwrap", path="/usr/bin:/bin"):
        with pytest.raises(RuntimeError, match="Bubblewrap"):
            await atelier.engine.run(recipe, {}, working_dir=root)
        return
    await atelier.engine.run(recipe, {}, working_dir=root)
    assert all(
        (root / role).read_text().strip() == "blocked" for role in ("build", "review", "test")
    )


def test_targets_are_user_only_and_source_paths_stay_in_project(
    target_config,
    tmp_path,
    monkeypatch,
    _isolate_global_atelier_dir,
):
    from pydantic import ValidationError

    from flow_atelier.core.settings import AtelierSettings
    from flow_atelier.schemas.deployment import DeploymentTarget

    monkeypatch.chdir(tmp_path)
    malicious = {str(tmp_path): {"elsewhere": target_config}}
    (tmp_path / ".atelier").mkdir()
    (tmp_path / ".atelier/settings.json").write_text(json.dumps({"deploymentTargets": malicious}))
    monkeypatch.setenv("ATELIER_DEPLOYMENT_TARGETS", json.dumps(malicious))
    assert AtelierSettings().deployment_targets == {}
    for fields in (
        {"sources": ["../outside"]},
        {"report_url": "http://unsigned"},
        {"deploy_account": "deployer@other-project.iam.gserviceaccount.com"},
    ):
        with pytest.raises(ValidationError):
            DeploymentTarget(**{**target_config, **fields})


def test_release_source_refuses_private_or_linked_files(tmp_path):
    from flow_atelier.services.executor.deployment import _snapshot

    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("fixture")
    (project / "linked").symlink_to(secret)
    (project / ".env").write_text("fixture")
    for name in ["linked", ".env"]:
        with pytest.raises(ValueError):
            _snapshot(project, [name], source)
    assert not list(source.iterdir())


def test_unsupported_sandbox_refuses_to_launch(tmp_path, monkeypatch):
    from flow_atelier.services.executor import cloud_sandbox

    monkeypatch.setattr(cloud_sandbox.sys, "platform", "unsupported")
    with pytest.raises(RuntimeError, match="sandbox"):
        cloud_sandbox.cloud_sandbox(["fixture-agent"], {}, tmp_path, tmp_path)


async def test_adapter_requires_persisted_human_gate_and_reuses_receipt(
    tmp_path, target_config, monkeypatch
):
    from flow_atelier.core.settings import AtelierSettings
    from flow_atelier.schemas.conduit import TaskDefinition
    from flow_atelier.schemas.log import LogEntry
    from flow_atelier.services.executor import deployment
    from flow_atelier.services.executor.base import FlowContext

    class FakeCloud:
        calls = 0

        def plan(self, destination, configured):
            return {"action": "Release fixture", "service": destination.service}

        async def deploy(self, destination, target, source):
            self.calls += 1
            assert target == "staging" and (source / "app.py").is_file()

    fake = FakeCloud()
    monkeypatch.setitem(deployment.ADAPTERS, "cloud-run", fake)
    settings = AtelierSettings(deployment_targets={str(tmp_path): {"staging": target_config}})
    app = Atelier(settings=settings)
    (tmp_path / "app.py").write_text("print('fixture')")
    run = app.store.create_flow("idea-to-product", {}, project=tmp_path)
    ctx = FlowContext(
        flow_id=run,
        working_dir=tmp_path,
        store=app.store,
        inputs={"deployment_target": "staging", "deploy_decision": "DEPLOY"},
    )
    task = TaskDefinition(
        name="deploy", description="Deploy", tool="tool:product-deployment", task="deploy"
    )
    service = app.executors[task.tool]
    await service.execute(task, "prepare", ctx)
    plan = (await service.execute(task, "plan", ctx)).output
    ctx.task_outputs.update(deployment_plan=plan, deployment_approval="deploy_decision: DEPLOY")
    with pytest.raises(ValueError, match="human approval"):
        await service.execute(task, "deploy", ctx)
    assert not fake.calls
    entry = LogEntry(
        task="deployment_approval",
        tool="tool:hitl",
        command=plan,
        output="deploy_decision: DEPLOY",
        started_at="fixture",
        finished_at="fixture",
    )
    await app.store.append_log(run, entry)
    first = await service.execute(task, "deploy", ctx)
    again = await service.execute(task, "deploy", ctx)
    assert first == again and fake.calls == 1
