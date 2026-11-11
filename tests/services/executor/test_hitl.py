"""HitlExecutor tests."""
import asyncio
import builtins

import pytest
import yaml

from flow_atelier.schemas.conduit import TaskDefinition, ToolType
from flow_atelier.services.executor.base import FlowContext
from flow_atelier.services.executor.hitl import HitlExecutor
from flow_atelier.services.store.filesystem import FilesystemStore


@pytest.fixture
def store(tmp_path):
    """Filesystem-backed store fixture pre-seeded with a hello conduit.

    :param tmp_path: pytest temp directory fixture.
    """
    s = FilesystemStore(tmp_path / ".atelier")
    conduit_dir = s.base_dir / "conduits" / "hello"
    conduit_dir.mkdir(parents=True)
    (conduit_dir / "conduit.yaml").write_text(
        "name: hello\ndescription: d\ntasks:\n  - t: {description: d, task: echo, tool: tool:bash, depends_on: []}\n"
    )
    return s


def _task() -> TaskDefinition:
    """Build a HITL TaskDefinition that asks for two inputs."""
    return TaskDefinition(
        name="ask",
        description="d",
        task="I need some details:",
        tool=ToolType.hitl,
        depends_on=[],
        inputs={"confirm": "type yes", "notes": "any notes"},
    )


async def test_hitl_collects_and_persists(store, monkeypatch, capsys):
    """Verify HITL collects answers, persists them, and prints the prompt.

    :param store: filesystem store fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :param capsys: pytest stdout/stderr capture fixture.
    """
    flow_id = store.create_flow("hello", {"env": "staging"})
    ctx = FlowContext(flow_id=flow_id, store=store, inputs={"env": "staging"})

    answers = iter(["yes", "all good"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    result = await HitlExecutor().execute(_task(), "I need some details:", ctx)

    parsed_output = yaml.safe_load(result.output)
    assert parsed_output == {"confirm": "yes", "notes": "all good"}

    assert ctx.inputs["confirm"] == "yes"
    assert ctx.inputs["notes"] == "all good"

    on_disk = yaml.safe_load(
        (store._flow_dir(flow_id) / "input.yaml").read_text()
    )
    assert on_disk["confirm"] == "yes"
    assert on_disk["notes"] == "all good"
    assert on_disk["env"] == "staging"

    captured = capsys.readouterr()
    assert "I need some details" in captured.out
    assert "needs the following inputs" in captured.out


async def test_hitl_exhausted_input_returns_friendly_error(store, monkeypatch):
    """Verify HITL returns a friendly result, not a bare EOFError, when the
    input stream runs dry (e.g. a scripted run short on answers).

    :param store: filesystem store fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    flow_id = store.create_flow("hello", {})
    ctx = FlowContext(flow_id=flow_id, store=store, inputs={})

    def _raise_eof(prompt=""):
        """Stand in for ``input`` exhausted of data by raising ``EOFError``."""
        raise EOFError

    monkeypatch.setattr(builtins, "input", _raise_eof)

    result = await HitlExecutor().execute(_task(), "I need some details:", ctx)

    assert result.exit_code == 1
    assert result.stderr == "interactive input unavailable: EOFError"
    assert result.output == ""


async def test_hitl_overwrite_collision(store, monkeypatch):
    """Verify a new HITL answer overwrites a prior value for the same key.

    :param store: filesystem store fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    flow_id = store.create_flow("hello", {"confirm": "previous"})
    ctx = FlowContext(flow_id=flow_id, store=store, inputs={"confirm": "previous"})
    monkeypatch.setattr(builtins, "input", lambda prompt="": "new")
    await HitlExecutor().execute(
        TaskDefinition(
            name="ask",
            description="d",
            task="",
            tool=ToolType.hitl,
            depends_on=[],
            inputs={"confirm": "again?"},
        ),
        "",
        ctx,
    )
    data = yaml.safe_load((store._flow_dir(flow_id) / "input.yaml").read_text())
    assert data["confirm"] == "new"
    assert ctx.inputs["confirm"] == "new"


async def test_hitl_hint_says_enter_submits(store, monkeypatch):
    """Each gate question shows the hint for the keys that really work.

    :param store: filesystem store fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    import flow_atelier.cli.rendering.multiline_input as _ml

    hints: list[str] = []

    async def fake_multiline_input(prompt: str, hint: str = "") -> str:
        """Record the hint and answer at once.

        :param prompt: prompt string (ignored).
        :param hint: the hint shown above the prompt.
        """
        hints.append(hint)
        return "yes"

    monkeypatch.setattr(_ml, "multiline_input", fake_multiline_input)
    fid = store.create_flow("hello", {})
    ctx = FlowContext(flow_id=fid, store=store, inputs={})
    result = await HitlExecutor().execute(_task(), "", ctx)
    assert result.exit_code == 0
    assert hints == [_ml.SUBMIT_HINT, _ml.SUBMIT_HINT]
    assert _ml.SUBMIT_HINT.startswith("Enter to submit")


async def test_concurrent_hitl_prompts_do_not_interleave(store, monkeypatch):
    """Two HITL tasks launched concurrently must not interleave their stdin
    prompts: the second session may only begin after the first fully ends.

    :param store: filesystem store fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    events: list[str] = []

    async def fake_multiline_input(prompt: str, hint: str = "") -> str:
        """Record prompt entry/exit keyed by the task tag in the prompt.

        :param prompt: prompt string (carries the input's name).
        :param hint: ignored submit hint.
        """
        tag = prompt.strip().split("_", 1)[0]
        events.append(f"{tag}:enter")
        await asyncio.sleep(0)  # yield: let the other coroutine run if it can
        events.append(f"{tag}:exit")
        return "x"

    monkeypatch.setattr(
        "flow_atelier.cli.rendering.multiline_input.multiline_input",
        fake_multiline_input,
    )

    def _hitl_task(tag: str) -> TaskDefinition:
        return TaskDefinition(
            name=f"ask_{tag}", description="d", task="", tool=ToolType.hitl,
            depends_on=[], inputs={f"{tag}_a": "d", f"{tag}_b": "d"},
        )

    fid1 = store.create_flow("hello", {})
    fid2 = store.create_flow("hello", {})
    ctx1 = FlowContext(flow_id=fid1, store=store, inputs={})
    ctx2 = FlowContext(flow_id=fid2, store=store, inputs={})

    await asyncio.gather(
        HitlExecutor().execute(_hitl_task("t1"), "", ctx1),
        HitlExecutor().execute(_hitl_task("t2"), "", ctx2),
    )

    # Each task owns the terminal for its whole session: one task's four
    # events must be contiguous, not split by the other's.
    first_tag = events[0].split(":", 1)[0]
    boundary = next(i for i, e in enumerate(events) if not e.startswith(first_tag))
    assert all(e.startswith(first_tag) for e in events[:boundary])
    assert all(not e.startswith(first_tag) for e in events[boundary:])
