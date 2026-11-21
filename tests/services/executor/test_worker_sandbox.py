import asyncio
import shutil
import sys

import pytest

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
        async with WorkerSandbox(workspace) as worker:
            # The real harness has completed its policy handshake; no model call is needed.
            result = await worker.command(
                [
                    "/bin/sh",
                    "-c",
                    """
                test -x /usr/bin/curl && test -z "$GH_TOKEN" && ! cat "$1" && ! cat "$2" &&
                ! /usr/bin/curl -sS --max-time 1 --data-binary @fixture-report "$3" && echo verified > permitted
            """,
                    "fixture",
                    str(secret),
                    str(auth / "auth.json"),
                    f"http://127.0.0.1:{port}/",
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
