"""Harness connection check — is the agent command reachable and ACP-capable?

flow-atelier never installs an agent and never logs one in, so the probe's
whole job is to report which of those the user still has to do.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from flow_atelier.schemas.conduit import TaskDefinition
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.harness import AcpHarnessExecutor

FAKE_AGENT = Path(__file__).resolve().parents[2] / "fixtures" / "fake_acp_agent.py"


def _agent(script: dict[str, Any]) -> AcpHarnessExecutor:
    """Build an executor pointed at the scripted fake ACP agent.

    :param script: the fake agent's scenario.
    """
    return AcpHarnessExecutor(
        launch_cmd=[sys.executable, str(FAKE_AGENT), "--script", json.dumps(script)]
    )


class TestReachableAgent:
    async def test_handshake_reports_agent_protocol_and_modes(self) -> None:
        """A working agent reports its identity, ACP version and modes."""
        result = await _agent(
            {
                "turns": [],
                "modes": {
                    "current": "default",
                    "available": [
                        {"id": "default", "name": "Default"},
                        {"id": "bypassPermissions", "name": "Bypass"},
                    ],
                },
            }
        ).probe(timeout=30)
        assert result.ok
        assert result.stage == "ok"
        assert result.agent == "fake-acp-agent 0.0.1"
        assert result.protocol_version == 1
        assert result.modes == ["default", "bypassPermissions"]
        # Naming the mode a real run would pick makes the check predictive.
        assert result.permissive_mode == "bypassPermissions"

    async def test_models_are_reported(self) -> None:
        """The probe lists the models a session offers and the default."""
        result = await _agent(
            {
                "turns": [],
                "models": {
                    "current": "m1",
                    "available": [{"id": "m1", "name": "One"}, {"id": "m2", "name": "Two"}],
                },
            }
        ).probe()
        assert result.ok
        assert result.models == ["m1", "m2"]
        assert result.current_model == "m1"

    async def test_advertised_auth_methods_are_reported_not_used(self) -> None:
        """Auth methods are surfaced as information; the probe never logs in."""
        result = await _agent(
            {"turns": [], "auth_methods": [{"id": "oauth", "name": "OAuth"}]}
        ).probe(timeout=30)
        assert result.ok
        assert result.auth_methods == ["oauth"]

    async def test_probe_sends_no_prompt(self) -> None:
        """The check must not consume a turn — it costs no tokens.

        The fake agent pops one scripted turn per prompt, so an untouched
        script proves nothing was sent.
        """
        executor = _agent({"turns": [{"chunks": ["should never run"]}]})
        assert (await executor.probe(timeout=30)).ok
        task = TaskDefinition(name="t", description="d", task="hi", tool="harness:x")
        context = FlowContext(flow_id="f", store=None, inputs={}, timeout=30)
        result = await executor.execute(task, "hi", context)
        assert "should never run" in result.output


class TestMissingSetup:
    async def test_missing_command_is_reported_without_spawning(self) -> None:
        """An uninstalled agent stops at the PATH check with its binary named."""
        result = await AcpHarnessExecutor(launch_cmd=["totally-not-installed"]).probe(
            timeout=5
        )
        assert not result.ok
        assert result.stage == "path"
        assert "totally-not-installed" in result.detail

    async def test_command_that_does_not_speak_acp_is_reported(self) -> None:
        """A command that starts but isn't an ACP entry point fails at initialize."""
        result = await AcpHarnessExecutor(
            launch_cmd=[sys.executable, "-c", "import sys; sys.stderr.write('nope\\n')"]
        ).probe(timeout=20)
        assert not result.ok
        assert result.stage == "initialize"
        # The agent's own words are the most useful diagnostic we have.
        assert "nope" in result.stderr

    async def test_logged_out_agent_reports_session_failure_and_methods(self) -> None:
        """A logged-out agent gets past initialize and fails to open a session."""
        result = await _agent(
            {
                "turns": [],
                "auth_methods": [{"id": "oauth", "name": "OAuth"}],
                "fail_session": "not authenticated",
            }
        ).probe(timeout=30)
        assert not result.ok
        assert result.stage == "session"
        assert result.auth_methods == ["oauth"]
        # It spoke ACP, so the identity it already gave us is still reported.
        assert result.agent == "fake-acp-agent 0.0.1"

    async def test_silent_command_times_out(self) -> None:
        """A command that never handshakes is bounded by the timeout."""
        result = await AcpHarnessExecutor(
            launch_cmd=[sys.executable, "-c", "import time; time.sleep(30)"]
        ).probe(timeout=2)
        assert not result.ok
        assert result.stage == "handshake"
        assert "2s" in result.detail
