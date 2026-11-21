import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.worker import WorkerSandbox, WorkerUnavailable


@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"} or not shutil.which("codex"),
    reason="Codex OS sandbox unavailable",
)
async def test_actual_sandbox_blocks_credentials_and_local_canary(tmp_path, monkeypatch):
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    (workspace / "fixture-report").write_text("synthetic fixture report")
    fake_home = tmp_path / "fixture-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    auth = fake_home / ".codex"
    auth.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(auth))
    (auth / "auth.json").write_text('{"fixture":"not a real credential"}')
    secret = fake_home / ".claude" / "credential"
    secret.parent.mkdir()
    secret.write_text("canary")
    monkeypatch.setenv("GH_TOKEN", "fixture-publisher-token")
    hits = []

    async def received(reader, writer):
        hits.append(True)
        writer.close()

    async with await asyncio.start_server(received, "127.0.0.1", 0) as canary:
        port = canary.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory(dir="/tmp") as shared:
            canary_path = Path(shared) / "unrelated"
            canary_path.write_text("fixture")
            async with WorkerSandbox(workspace) as worker:
                # The real harness has completed its policy handshake; no model call is needed.
                result = await worker.command(
                    [
                        "/bin/sh",
                        "-c",
                        """
                        test -x /usr/bin/curl && test -z "$GH_TOKEN" &&
                        test ! -r "$4" && ! touch "$4.other" && ! cat "$1" && ! cat "$2" &&
                        ! /usr/bin/curl -sS --max-time 1 --data-binary @fixture-report "$3" &&
                        echo verified > permitted
                        """,
                        "fixture",
                        str(secret),
                        str(auth / "auth.json"),
                        f"http://127.0.0.1:{port}/",
                        str(canary_path),
                    ]
                )
                assert result.returncode == 0, result.stdout
                assert (workspace / "permitted").read_text().strip() == "verified"
    assert not hits


async def test_refuses_unavailable_harness_before_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(WorkerUnavailable, match="Codex"):
        async with WorkerSandbox(tmp_path):
            pytest.fail("an unavailable worker must never receive task data")


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("codex"), reason="native macOS Codex")
async def test_product_reviewer_can_list_target_without_writing(tmp_path, monkeypatch):
    workspace = tmp_path / "product"
    workspace.mkdir()
    (workspace / "acceptance.txt").write_text("read me")
    auth = tmp_path / "fake-login"
    auth.mkdir()
    (auth / "auth.json").write_text('{"fixture":"fake login"}')
    monkeypatch.setenv("CODEX_HOME", str(auth))
    cloud = workspace / "local-cloud"
    cloud.mkdir()
    (cloud / "fixture").write_text("private")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(cloud))

    async def inspect(reviewer, prompt, **kwargs):
        assert reviewer.read_only and reviewer.workspace == workspace.resolve()
        result = await reviewer.command(["/bin/ls", "acceptance.txt"])
        assert result.returncode == 0 and result.stdout.strip() == "acceptance.txt"
        assert (await reviewer.command(["/bin/cat", "acceptance.txt"])).stdout == "read me"
        assert (await reviewer.command(["/usr/bin/touch", "forbidden"])).returncode != 0
        assert (await reviewer.command(["/bin/cat", str(cloud / "fixture")])).returncode != 0
        return "PASS"

    # Exercise the production binding and OS policy, substituting only the model turn.
    monkeypatch.setattr(WorkerSandbox, "turn", inspect)
    atlas = Atelier()
    task = TaskDefinition(name="review", description="Review", task="Inspect", tool="harness:codex",
                          permissions="read-only", deny_cloud_config=True)
    result = await atlas.executors[task.tool].execute(
        task, task.task, FlowContext(flow_id="review", store=atlas.store, inputs={},
                                    working_dir=workspace),
    )
    assert result.output == "PASS"
    assert sorted(p.name for p in workspace.iterdir()) == ["acceptance.txt", "local-cloud"]
