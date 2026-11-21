"""End-to-end integration tests (no live harnesses)."""
import builtins
import sys

import pytest

from flow_atelier.core.atelier import Atelier


def _write_conduit(base, name, body):
    """Write a conduit YAML under `base/.atelier/conduits/<name>/conduit.yaml`.

    :param base: base working directory path.
    :param name: conduit name.
    :param body: YAML body to write.
    """
    d = base / ".atelier" / "conduits" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "conduit.yaml").write_text(body)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Provide an isolated working directory for integration tests.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_bash_pipeline_with_conditional_branch(workdir):
    """Verify a bash pipeline with conditional branches routes correctly.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "pipeline",
        """
name: pipeline
description: bash end-to-end
inputs:
  name: who to greet
tasks:
  - hello:
      description: greet
      task: "echo hello {{inputs.name}}"
      tool: tool:bash
      depends_on: []
  - verdict:
      description: emit verdict
      task: "echo 'VERDICT: APPROVE'"
      tool: tool:bash
      depends_on: [hello]
  - deploy:
      description: deploy path
      task: "echo deploying"
      tool: tool:bash
      depends_on:
        - verdict.output.match(APPROVE)
  - rollback:
      description: rollback path
      task: "echo rolling-back"
      tool: tool:bash
      depends_on:
        - verdict.output.not_match(APPROVE)
""",
    )
    a = Atelier()
    flow_id = await a.run_conduit("pipeline", {"name": "world"})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"
    assert p.tasks["deploy"].status.value == "completed"
    assert p.tasks["rollback"].status.value == "skipped"

    logs = [e.model_dump() for e in a.store.read_logs(flow_id)]
    hello_log = next(e for e in logs if e["task"] == "hello")
    assert "hello world" in hello_log["output"]


async def test_repeat_creates_multiple_log_entries(workdir):
    """Verify a repeated task emits one log entry per iteration.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "rep",
        """
name: rep
description: repeat
tasks:
  - tick:
      description: tick
      task: "echo tick"
      tool: tool:bash
      depends_on: []
      repeat: 4
""",
    )
    a = Atelier()
    flow_id = await a.run_conduit("rep", {})
    logs = [e.model_dump() for e in a.store.read_logs(flow_id)]
    ticks = [l for l in logs if l["task"] == "tick"]
    assert len(ticks) == 4
    assert [l["iteration"] for l in ticks] == [1, 2, 3, 4]


async def test_hitl_feeds_downstream_inputs(workdir, monkeypatch):
    """Verify human-in-the-loop answers flow into downstream tasks.

    :param workdir: isolated working directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    """
    _write_conduit(
        workdir,
        "askflow",
        """
name: askflow
description: hitl flow
tasks:
  - ask:
      description: ask human
      task: "Please provide details"
      tool: tool:hitl
      depends_on: []
      inputs:
        username: your name
        confirm: yes/no
  - greet:
      description: greet
      task: "echo '{{inputs.username}} says {{inputs.confirm}}'"
      tool: tool:bash
      depends_on: [ask]
""",
    )
    answers = iter(["alice", "yes"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))

    a = Atelier()
    flow_id = await a.run_conduit("askflow", {})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"

    logs = [e.model_dump() for e in a.store.read_logs(flow_id)]
    greet_log = next(l for l in logs if l["task"] == "greet")
    assert "alice says yes" in greet_log["output"]

    input_yaml = a.store.read_input(flow_id)
    assert input_yaml["username"] == "alice"
    assert input_yaml["confirm"] == "yes"


async def test_nested_conduit(workdir):
    """Verify a parent conduit can invoke a nested child conduit.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "child",
        """
name: child
description: nested child
inputs:
  label: label
tasks:
  - step:
      description: step
      task: "echo child-{{inputs.label}}"
      tool: tool:bash
      depends_on: []
""",
    )
    _write_conduit(
        workdir,
        "parent",
        """
name: parent
description: calls child
tasks:
  - setup:
      description: setup
      task: "echo setup-done"
      tool: tool:bash
      depends_on: []
  - nested:
      description: run child
      task: child
      tool: tool:conduit
      depends_on: [setup]
      inputs:
        label: "{{setup.output}}"
""",
    )
    a = Atelier()
    flow_id = await a.run_conduit("parent", {})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"
    child_flows = a.store.list_child_flows(flow_id)
    assert len(child_flows) == 1
    child_dir = child_flows[0]
    child_logs = [e.model_dump() for e in a.store.read_logs(child_dir)]
    assert any("child-setup-done" in e["output"] for e in child_logs)


async def test_fail_fast_flow_failed_status(workdir):
    """Verify a failing task marks the whole flow as failed.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "fast",
        """
name: fast
description: ff
tasks:
  - bad:
      description: fails
      task: "exit 1"
      tool: tool:bash
      depends_on: []
  - good:
      description: good
      task: "echo hi"
      tool: tool:bash
      depends_on: []
""",
    )
    a = Atelier()
    with pytest.raises(RuntimeError):
        await a.run_conduit("fast", {})
    flow_id = a.list_flows("fast")[0]
    p = a.get_status(flow_id)
    assert p.status.value == "failed"
    assert p.tasks["bad"].status.value == "failed"


@pytest.mark.skipif(sys.platform == "win32", reason="uses Unix tr/wc pipes")
async def test_until_early_exit_with_real_bash(workdir):
    """Verify `until:` exits the repeat loop when the predicate matches.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "poller",
        """
name: poller
description: poll with early exit
tasks:
  - poll:
      description: echo HIT on the 3rd call
      task: "echo x >> counter.log; n=$(wc -l < counter.log | tr -d ' '); if [ \\"$n\\" -ge 3 ]; then echo HIT; else echo wait; fi"
      tool: tool:bash
      depends_on: []
      repeat: 5
      until: "output.match(HIT)"
""",
    )
    a = Atelier()
    flow_id = await a.run_conduit("poller", {})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"
    assert p.tasks["poll"].status.value == "completed"
    assert p.tasks["poll"].iteration == 3
    assert p.tasks["poll"].of == 5

    logs = [e.model_dump() for e in a.store.read_logs(flow_id)]
    poll_logs = [l for l in logs if l["task"] == "poll"]
    assert len(poll_logs) == 3
    assert "HIT" in poll_logs[-1]["output"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses Unix tr/wc pipes")
async def test_while_early_exit_with_real_bash(workdir):
    """`while: output.match(retry)` keeps iterating while bash emits
    "retry" and breaks the iteration that emits something else.

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "retry_loop",
        """
name: retry_loop
description: while early-exit
tasks:
  - poll:
      description: emit retry until the 3rd call
      task: "echo x >> counter.log; n=$(wc -l < counter.log | tr -d ' '); if [ \\"$n\\" -lt 3 ]; then echo retry; else echo settled; fi"
      tool: tool:bash
      depends_on: []
      repeat: 5
      while: "output.match(retry)"
""",
    )
    a = Atelier()
    flow_id = await a.run_conduit("retry_loop", {})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"
    assert p.tasks["poll"].status.value == "completed"
    assert p.tasks["poll"].iteration == 3
    assert p.tasks["poll"].of == 5

    logs = [e.model_dump() for e in a.store.read_logs(flow_id)]
    poll_logs = [l for l in logs if l["task"] == "poll"]
    assert len(poll_logs) == 3
    assert "retry" in poll_logs[0]["output"]
    assert "settled" in poll_logs[-1]["output"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses Unix tr/wc pipes")
async def test_until_over_nested_conduit_sub_task_outputs(workdir):
    """`tool:conduit` task with `until: output.match(PASS)` stops when a
    nested sub-task emits PASS, even though the conduit's aggregate
    output never contains PASS.

    Vacuousness guard (per SPEC §5): the child's last sub-task always
    echoes the fixed string "nested conduit completed", which becomes
    the conduit's aggregate ``output``. Only the engine's conduit-scope
    predicate (sub_outputs) can see the earlier ``tests`` task's
    "PASS". Confirmed to fail on a build of the engine without the
    conduit-scope wiring (the outer loop runs all 5 iterations).

    :param workdir: isolated working directory fixture.
    """
    _write_conduit(
        workdir,
        "build_and_test",
        """
name: build_and_test
description: counter-driven test child
tasks:
  - tests:
      description: emit PASS on the 3rd outer iteration
      task: "echo x >> counter.log; n=$(wc -l < counter.log | tr -d ' '); if [ \\"$n\\" -ge 3 ]; then echo PASS; else echo RETRY; fi"
      tool: tool:bash
      depends_on: []
  - finalize:
      description: fixed terminal output
      task: "echo 'nested conduit completed'"
      tool: tool:bash
      depends_on: [tests]
""",
    )
    _write_conduit(
        workdir,
        "outer_runner",
        """
name: outer_runner
description: outer loop over a nested conduit
tasks:
  - nested:
      description: run build_and_test until tests pass
      task: build_and_test
      tool: tool:conduit
      depends_on: []
      repeat: 5
      until: "output.match(PASS)"
""",
    )

    a = Atelier()
    flow_id = await a.run_conduit("outer_runner", {})
    p = a.get_status(flow_id)
    assert p.status.value == "completed"
    assert p.tasks["nested"].status.value == "completed"
    assert p.tasks["nested"].iteration == 3
    assert p.tasks["nested"].of == 5

    child_flows = a.store.list_child_flows(flow_id)
    assert len(child_flows) == 3, child_flows

    # Confirm exactly one child flow's `tests` sub-task emitted PASS
    # while every `finalize` sub-task emitted only the fixed terminal
    # string. Together this proves the predicate fired on a sub-output,
    # not on the aggregate output.
    pass_count = 0
    for child_dir in child_flows:
        child_logs = [e.model_dump() for e in a.store.read_logs(child_dir)]
        tests_log = next(l for l in child_logs if l["task"] == "tests")
        finalize_log = next(l for l in child_logs if l["task"] == "finalize")
        if "PASS" in tests_log["output"]:
            pass_count += 1
        assert "PASS" not in finalize_log["output"]
        assert "nested conduit completed" in finalize_log["output"]
    assert pass_count == 1


async def test_concurrency_cap_honored(workdir):
    """Verify max_concurrency caps parallel task execution.

    :param workdir: isolated working directory fixture.
    """
    # four sleeps with cap 2 should take >= 2 * sleep
    _write_conduit(
        workdir,
        "par",
        """
name: par
description: cap
max_concurrency: 2
tasks:
  - a:
      description: a
      task: "sleep 0.3"
      tool: tool:bash
      depends_on: []
  - b:
      description: b
      task: "sleep 0.3"
      tool: tool:bash
      depends_on: []
  - c:
      description: c
      task: "sleep 0.3"
      tool: tool:bash
      depends_on: []
  - d:
      description: d
      task: "sleep 0.3"
      tool: tool:bash
      depends_on: []
""",
    )
    import time
    a = Atelier()
    t0 = time.monotonic()
    await a.run_conduit("par", {})
    elapsed = time.monotonic() - t0
    # With cap=2 and 4 tasks at 0.3s each, expected ≥ 0.6s
    assert elapsed >= 0.55
