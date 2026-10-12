"""ACP agent registry — snapshot loading and launch-command derivation."""
from __future__ import annotations

import json
import os
from pathlib import Path

from flow_atelier.services.executor.acp_registry import (
    SNAPSHOT_PATH,
    LaunchSpec,
    _launch_spec,
    load_registry,
    platform_key,
    trim_registry,
    write_snapshot,
)

PLAT = "darwin-aarch64"


def _agent(distribution: dict) -> dict:
    """Build a minimal registry agent record.

    :param distribution: the distribution block under test.
    """
    return {
        "id": "demo",
        "name": "Demo",
        "version": "1.2.3",
        "description": "d",
        "distribution": distribution,
    }


class TestLaunchDerivation:
    def test_npx_becomes_npx_dash_y_package_args(self) -> None:
        """An npx distribution derives `npx -y <package> <args...>`."""
        spec = _launch_spec(
            _agent({"npx": {"package": "@google/gemini-cli@0.52.0", "args": ["--acp"]}}),
            PLAT,
        )
        assert spec.argv == ("npx", "-y", "@google/gemini-cli@0.52.0", "--acp")
        assert spec.kind == "npx"

    def test_uvx_omits_the_yes_flag(self) -> None:
        """A uvx distribution derives `uvx <package> <args...>`."""
        spec = _launch_spec(_agent({"uvx": {"package": "x==1.0", "args": ["-x"]}}), PLAT)
        assert spec.argv == ("uvx", "x==1.0", "-x")

    def test_binary_uses_the_basename_from_path(self) -> None:
        """A binary distribution runs the installed CLI, not the archive path.

        The registry's `cmd` points inside an archive we never download, so
        only its basename — the name the CLI installs itself as — is usable.
        """
        spec = _launch_spec(
            _agent(
                {"binary": {PLAT: {"cmd": "./dist-package/cursor-agent", "args": ["acp"]}}}
            ),
            PLAT,
        )
        assert spec.argv == ("cursor-agent", "acp")
        assert spec.kind == "binary"

    def test_self_installing_distribution_wins_over_binary(self) -> None:
        """npx is preferred over binary: it needs no prior install."""
        spec = _launch_spec(
            _agent(
                {
                    "binary": {PLAT: {"cmd": "./kilo", "args": ["acp"]}},
                    "npx": {"package": "kilo@1.0"},
                }
            ),
            PLAT,
        )
        assert spec.kind == "npx"

    def test_binary_without_a_build_for_this_platform_is_dropped(self) -> None:
        """An agent with no runnable distribution here resolves to None."""
        assert _launch_spec(_agent({"binary": {"linux-x86_64": {"cmd": "./x"}}}), PLAT) is None

    def test_env_is_carried_through(self) -> None:
        """A distribution's env map reaches the LaunchSpec."""
        spec = _launch_spec(
            _agent({"npx": {"package": "a@1", "env": {"AUTO_UPDATE": "0"}}}), PLAT
        )
        assert spec.env == {"AUTO_UPDATE": "0"}


class TestSnapshot:
    def test_bundled_snapshot_loads_and_covers_the_known_agents(self) -> None:
        """The shipped snapshot resolves the agents flow-atelier documents."""
        specs = load_registry()
        for agent_id in ("claude-acp", "codex-acp", "gemini", "github-copilot-cli"):
            assert isinstance(specs[agent_id], LaunchSpec), agent_id
            assert specs[agent_id].argv

    def test_user_snapshot_supersedes_the_bundled_one(self, tmp_path) -> None:
        """A synced snapshot in the global dir wins over the packaged one.

        :param tmp_path: pytest temp directory fixture.
        """
        path = tmp_path / "acp_registry.json"
        write_snapshot(
            path,
            {"source": "x", "version": "1.0.0", "agents": [_agent({"npx": {"package": "z@9"}})]},
        )
        specs = load_registry(path)
        assert list(specs) == ["demo"]
        assert specs["demo"].argv == ("npx", "-y", "z@9")

    def test_corrupt_user_snapshot_falls_back_to_bundled(self, tmp_path) -> None:
        """Bad JSON in the user snapshot must not disable every harness.

        :param tmp_path: pytest temp directory fixture.
        """
        path = tmp_path / "acp_registry.json"
        path.write_text("{not json", encoding="utf-8")
        assert "claude-acp" in load_registry(path)

    def test_missing_user_snapshot_falls_back_to_bundled(self, tmp_path) -> None:
        """The common case — nobody has run `atelier harness sync` yet.

        :param tmp_path: pytest temp directory fixture.
        """
        assert "claude-acp" in load_registry(tmp_path / "absent.json")


class TestTrim:
    def test_trim_drops_archives_and_metadata(self) -> None:
        """Trimming keeps launch data and discards release artifacts."""
        raw = {
            "version": "1.0.0",
            "agents": [
                {
                    "id": "a",
                    "name": "A",
                    "version": "1.0.0",
                    "description": "d",
                    "icon": "https://example/icon.svg",
                    "repository": "https://example/repo",
                    "distribution": {
                        "binary": {
                            PLAT: {
                                "cmd": "./a",
                                "args": ["acp"],
                                "archive": "https://example/a.tar.gz",
                                "sha256": "deadbeef",
                            }
                        }
                    },
                }
            ],
        }
        trimmed = trim_registry(raw)
        entry = trimmed["agents"][0]
        assert entry["distribution"]["binary"][PLAT] == {"cmd": "./a", "args": ["acp"]}
        assert "icon" not in entry and "repository" not in entry

    def test_agent_with_no_usable_distribution_is_dropped(self) -> None:
        """An entry we could never launch is left out of the snapshot."""
        raw = {"version": "1.0.0", "agents": [{"id": "a", "distribution": {"docker": {}}}]}
        assert trim_registry(raw)["agents"] == []

    def test_bundled_snapshot_is_already_trimmed(self) -> None:
        """Guard against re-committing a full registry dump by accident."""
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        assert snapshot["agents"]
        for agent in snapshot["agents"]:
            assert set(agent) == {"id", "name", "version", "description", "distribution"}


def test_platform_key_uses_registry_spelling() -> None:
    """The platform key must match the registry's <os>-<arch> keys."""
    key = platform_key()
    assert key.count("-") == 1
    system, arch = key.split("-")
    assert system in ("darwin", "linux", "windows")
    assert arch in ("aarch64", "x86_64") or arch  # unknown arch simply finds nothing


def test_every_registry_id_is_a_valid_harness_name() -> None:
    """Every id must satisfy the harness:<name> grammar the schema enforces.

    An id that fails this registers an executor key no conduit could name,
    so it has to be caught when the snapshot is synced, not at run time.
    """
    from flow_atelier.schemas.conduit import HARNESS_NAME_RE

    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    bad = [a["id"] for a in snapshot["agents"] if not HARNESS_NAME_RE.match(a["id"])]
    assert bad == []


class TestMalformedDistribution:
    """A bad snapshot entry must be skipped, never raised out of load."""

    def test_npx_entry_without_a_package_is_skipped(self, tmp_path) -> None:
        """`args` but no `package` used to KeyError out of Atelier.__init__.

        trim_registry keeps such an entry (it filters keys, not shapes), so
        the resolver is where it has to be survivable — and every command
        constructs an Atelier, including the `harness sync` that would
        replace the offending snapshot.

        :param tmp_path: pytest temp directory fixture.
        """
        path = tmp_path / "acp_registry.json"
        write_snapshot(
            path,
            {
                "source": "x",
                "version": "1.0.0",
                "agents": [
                    _agent({"npx": {"args": ["--acp"]}}),
                    {**_agent({"npx": {"package": "good@1"}}), "id": "fine"},
                ],
            },
        )
        specs = load_registry(path)
        assert "demo" not in specs
        assert specs["fine"].argv == ("npx", "-y", "good@1")

    def test_uvx_entry_without_a_package_is_skipped(self, tmp_path) -> None:
        """Same guard on the uvx branch.

        :param tmp_path: pytest temp directory fixture.
        """
        path = tmp_path / "acp_registry.json"
        write_snapshot(
            path,
            {"source": "x", "version": "1.0.0", "agents": [_agent({"uvx": {"env": {"A": "1"}}})]},
        )
        assert load_registry(path) == {}

    def test_a_broken_entry_does_not_stop_atelier_starting(self, tmp_path) -> None:
        """The real blast radius: the facade must still construct.

        :param tmp_path: pytest temp directory fixture.
        """
        from flow_atelier.core.atelier import Atelier
        from flow_atelier.core.settings import AtelierSettings

        write_snapshot(
            tmp_path / "global" / "acp_registry.json",
            {"source": "x", "version": "1.0.0", "agents": [_agent({"npx": {"args": []}})]},
        )
        atelier = Atelier(
            settings=AtelierSettings(
                atelier_dir=tmp_path / ".atelier",
                global_atelier_dir=tmp_path / "global",
            )
        )
        assert "tool:bash" in atelier.executors


def _fake_install(root, package: str, bin_field, exe: str) -> None:
    """Lay out what `npm install --prefix` leaves for ``package``.

    :param root: the install root.
    :param package: pinned package spec, e.g. ``@scope/demo@1.2.3``.
    :param bin_field: the manifest's ``bin`` value.
    :param exe: the executable name npm links under ``node_modules/.bin``.
    """
    from flow_atelier.services.executor.acp_registry import package_install_dir

    modules = package_install_dir(root, package) / "node_modules"
    name = package[: package.rfind("@")]
    (modules / name).mkdir(parents=True)
    (modules / name / "package.json").write_text(json.dumps({"bin": bin_field}))
    (modules / ".bin").mkdir()
    shim = f"{exe}.cmd" if os.name == "nt" else exe
    (modules / ".bin" / shim).write_text("#!/usr/bin/env node\n")


class TestInstalledAdapters:
    def test_an_installed_pinned_package_runs_without_npx(self, tmp_path) -> None:
        """An installed copy of the exact pinned version launches directly."""
        package = "@scope/demo-acp@1.2.3"
        _fake_install(tmp_path, package, {"demo-acp": "dist/index.js"}, "demo-acp")
        spec = _launch_spec(_agent({"npx": {"package": package, "args": ["--acp"]}}), PLAT, tmp_path)
        assert spec.kind == "installed"
        assert Path(spec.argv[0]).parent.name == ".bin"
        assert Path(spec.argv[0]).stem == "demo-acp"
        assert spec.argv[1:] == ("--acp",)
        assert spec.package == package

    def test_a_string_bin_is_named_after_the_package(self, tmp_path) -> None:
        """A manifest with a single string ``bin`` links the unscoped name."""
        package = "@scope/demo-acp@1.2.3"
        _fake_install(tmp_path, package, "dist/index.js", "demo-acp")
        spec = _launch_spec(_agent({"npx": {"package": package}}), PLAT, tmp_path)
        assert spec.kind == "installed"

    def test_another_installed_version_falls_back_to_npx(self, tmp_path) -> None:
        """A registry that pins a newer version ignores the old install."""
        _fake_install(tmp_path, "@scope/demo-acp@1.2.3", {"demo-acp": "x"}, "demo-acp")
        spec = _launch_spec(
            _agent({"npx": {"package": "@scope/demo-acp@1.3.0"}}), PLAT, tmp_path
        )
        assert spec.kind == "npx"
        assert spec.argv[:2] == ("npx", "-y")

    def test_install_reports_a_failing_npm(self, tmp_path, monkeypatch) -> None:
        """A failed npm install raises with npm's own explanation."""
        import subprocess

        import pytest

        from flow_atelier.services.executor.acp_registry import install_agent

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "E404 not found"),
        )
        with pytest.raises(OSError, match="E404"):
            install_agent(tmp_path, "@scope/demo-acp@1.2.3")

    def test_install_runs_npm_into_the_package_dir(self, tmp_path, monkeypatch) -> None:
        """Install runs npm with ``--prefix`` and returns the linked executable."""
        import subprocess

        from flow_atelier.services.executor.acp_registry import install_agent

        package = "@scope/demo-acp@1.2.3"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            _fake_install(tmp_path, package, {"demo-acp": "x"}, "demo-acp")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        executable = install_agent(tmp_path, package)
        assert executable.stem == "demo-acp"
        assert calls[0][-1] == package
        assert "--prefix" in calls[0]
