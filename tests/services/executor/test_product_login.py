import os
import sys
from unittest.mock import AsyncMock

import pytest

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.services.executor import cloud_sandbox
from flow_atelier.services.executor.base import FlowContext


@pytest.mark.parametrize("tool", ["harness:claude-code", "harness:claude-acp:opus"])
async def test_macos_claude_product_step_requires_setup_token(tmp_path, monkeypatch, tool):
    atlas = Atelier()
    executor = atlas.executors["harness:claude-code"]
    task = TaskDefinition(
        name="build", description="Build", task="Build", tool=tool, deny_cloud_config=True
    )
    context = FlowContext(flow_id="build", store=atlas.store, inputs={}, working_dir=tmp_path)
    drive = AsyncMock()
    monkeypatch.setattr(executor, "_drive_session", drive)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    result = await executor.execute(task, task.task, context)
    drive.assert_not_awaited()
    assert result.exit_code and "CLAUDE_CODE_OAUTH_TOKEN" in result.stderr
    assert "run claude setup-token" in result.stderr


@pytest.fixture(
    params=[
        ("harness:claude-code", "CLAUDE_CONFIG_DIR", ".claude", ".credentials.json"),
        ("harness:codex-acp", "CODEX_HOME", ".codex", "auth.json"),
    ]
)
def login(request, tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Product file sandbox requires POSIX")
    tool, variable, directory, name = request.param
    monkeypatch.setattr(sys, "platform", "linux")
    config = tmp_path / "real"
    config.mkdir()
    source = config / name
    source.write_bytes(b'{"fixture":"original"}')

    def step(scratch):
        scratch.mkdir()
        return (
            cloud_sandbox.product_login(tool, {variable: str(config)}, scratch),
            scratch / directory / name,
        )

    return source, step


def test_unchanged_and_refreshed_product_login(tmp_path, login):
    source, step = login
    original = source.stat()
    first, copy = step(tmp_path / "first")
    with first:
        assert copy.read_bytes() == source.read_bytes()
        assert not copy.samefile(source)
    assert os.path.samestat(source.stat(), original)
    assert source.stat().st_mtime_ns == original.st_mtime_ns

    second, copy = step(tmp_path / "second")
    with second:
        copy.write_bytes(b'{"fixture":"refreshed"}')
    assert source.read_bytes() == b'{"fixture":"refreshed"}'


def test_concurrent_product_refresh_preserves_first_commit(tmp_path, login):
    source, step = login
    older, old_copy = step(tmp_path / "older")
    newer, new_copy = step(tmp_path / "newer")
    with older:
        with newer:
            new_copy.write_bytes(b'{"fixture":"winner"}')
        old_copy.write_bytes(b'{"fixture":"stale"}')
    assert source.read_bytes() == b'{"fixture":"winner"}'


@pytest.mark.parametrize("link", ["symlink", "hardlink", "directory"])
def test_product_refresh_ignores_linked_copy(tmp_path, login, link):
    source, step = login
    original = source.read_bytes()
    session, copy = step(tmp_path / "scratch")
    with session:
        copy.unlink()
        target = tmp_path / copy.name
        target.write_bytes(b'{"fixture":"unrelated"}')
        if link == "hardlink":
            copy.hardlink_to(target)
        elif link == "directory":
            copy.parent.rmdir()
            copy.parent.symlink_to(tmp_path, target_is_directory=True)
        else:
            copy.symlink_to(target)
    assert source.read_bytes() == original


@pytest.mark.parametrize("replacement", ["symlink", "logout"])
def test_refresh_preserves_replaced_or_removed_real_login(tmp_path, login, replacement):
    source, step = login
    session, copy = step(tmp_path / "scratch")
    target = tmp_path / "other-login"
    target.write_bytes(source.read_bytes())
    with session:
        copy.write_bytes(b'{"fixture":"refreshed"}')
        source.unlink()
        if replacement == "symlink":
            source.symlink_to(target)
        else:
            source.parent.rmdir()
    assert source.is_symlink() if replacement == "symlink" else not source.exists()
    assert target.read_bytes() == b'{"fixture":"original"}'
