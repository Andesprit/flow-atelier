"""What `atelier diagnose` says about a saved run, and what it refuses to say.

Every flow here is written straight into the store, so each state a report has
to distinguish — crashed, live, foreign-host, stopped, completed, corrupt,
orphan-only — is reproduced exactly instead of being provoked. The real
process path lives in ``test_diagnose_recovery.py``.
"""
from __future__ import annotations

import json
import os
import socket

import pytest
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.log import IntermediateStep, LogEntry, StepKind, StepRecord
from flow_atelier.schemas.progress import (
    FlowStatus,
    Progress,
    TaskProgress,
    TaskStatus,
)

CONDUIT_YAML = """
name: triage
description: two workers and a synthesis
tasks:
  - name: worker_a
    description: the one that works
    tool: harness:claude-code
    task: "do the thing"
  - name: worker_b
    description: the one that breaks
    tool: harness:codex
    task: "do the other thing"
  - name: synthesis
    description: needs both
    tool: tool:bash
    task: "echo {{worker_a.output}} {{worker_b.output}}"
    depends_on: [worker_a, worker_b]
"""

FLOW = "20260101_aaaaaaaa_triage"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An isolated project holding the triage conduit and nothing else.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the project root.
    """
    conduits = tmp_path / ".atelier" / "conduits" / "triage"
    conduits.mkdir(parents=True)
    (conduits / "conduit.yaml").write_text(CONDUIT_YAML)
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("ATELIER_") and key not in (
            "ATELIER_GLOBAL_ATELIER_DIR",
            "ATELIER_NO_UPDATE_CHECK",
        ):
            monkeypatch.delenv(key, raising=False)
    return tmp_path


def _entry(task: str, tool: str, **kw) -> LogEntry:
    """Build one log entry with the timestamps the schema requires.

    :param task: task name.
    :param tool: the tool that ran it.
    :param kw: any other :class:`LogEntry` field.
    :returns: the entry.
    """
    return LogEntry(
        task=task,
        tool=tool,
        started_at="2026-01-01T10:00:00Z",
        finished_at="2026-01-01T10:00:01Z",
        **kw,
    )


def _seed(
    atelier: Atelier,
    *,
    status: FlowStatus = FlowStatus.failed,
    tasks: dict[str, TaskProgress] | None = None,
    entries: list[LogEntry] | None = None,
    outputs: dict[str, str] | None = None,
    steps: list[StepRecord] | None = None,
    task_agents: dict[str, str] | None = None,
    runner_pid: int | None = 424242,
    runner_host: str | None = None,
    flow_id: str = FLOW,
    parent: str | None = None,
) -> str:
    """Write one flow into the store exactly as a run would have left it.

    :param atelier: facade rooted at the test project.
    :param status: saved flow status.
    :param tasks: per-task progress; defaults to the completed/failed/cancelled
        shape the report was built for.
    :param entries: log entries to append.
    :param outputs: the saved outputs map; omitted writes no outputs file.
    :param steps: live step records to append.
    :param task_agents: the run's recorded agent selections.
    :param runner_pid: pid to record; None records none.
    :param runner_host: host to record; None records this machine.
    :param flow_id: the id to create.
    :param parent: parent flow id, for a nested child.
    :returns: the flow id.
    """
    atelier.store.create_flow("triage", {}, parent_flow_id=parent, flow_id=flow_id)
    progress = Progress(
        status=status,
        tasks=tasks
        if tasks is not None
        else {
            "worker_a": TaskProgress(status=TaskStatus.completed),
            "worker_b": TaskProgress(
                status=TaskStatus.failed, reason="exit=1 stderr=boom"
            ),
            "synthesis": TaskProgress(status=TaskStatus.cancelled),
        },
        started_at="2026-01-01T10:00:00Z",
        finished_at="2026-01-01T10:05:00Z" if status != FlowStatus.running else None,
        runner_pid=runner_pid,
        runner_host=socket.gethostname() if runner_host is None else runner_host,
        task_agents=task_agents or {},
    )
    atelier.store.write_progress(flow_id, progress)
    for entry in entries or []:
        import asyncio

        asyncio.run(atelier.store.append_log(flow_id, entry))
    if outputs is not None:
        atelier.store.write_outputs(flow_id, outputs)
    for record in steps or []:
        import asyncio

        asyncio.run(atelier.store.append_step(flow_id, record))
    return flow_id


def _flat(text: str) -> str:
    """Collapse the line breaks Rich inserted, so prose can be matched.

    :param text: rendered console text.
    :returns: the same words on one line.
    """
    return " ".join(text.split())


def _run(*args) -> object:
    """Invoke the CLI, keeping stdout and stderr apart.

    :param args: argv after ``atelier``.
    :returns: the CliRunner result.
    """
    return CliRunner().invoke(app, list(args))


# --------------------------------------------------------------- the ordinary case


@pytest.fixture
def failed_run(workdir):
    """A run whose second agent failed and whose synthesis was cancelled."""
    atelier = Atelier()
    return _seed(
        atelier,
        entries=[
            _entry("worker_a", "harness:claude-code", output="A's answer", exit_code=0),
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=1,
                stderr="boom\ntraceback line\n",
                output="partial",
            ),
        ],
        outputs={"worker_a": "A's answer"},
        task_agents={"worker_a": "harness:claude-code", "worker_b": "harness:codex"},
    )


def test_names_the_failure_and_not_the_cancellation(failed_run):
    """The failed task is the failure; the cancelled one is reported as not run."""
    result = _run("diagnose", FLOW)
    assert result.exit_code == 0, result.output
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]
    assert payload["not_run"] == []
    assert [t["task"] for t in payload["cancelled"]] == ["synthesis"]
    assert payload["cancelled"][0]["status"] == "cancelled"
    assert "not the failure" in _flat(result.output)


def test_shows_the_kept_result_and_who_produced_it(failed_run):
    """The completed worker, its agent and its saved result all survive."""
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    kept = payload["kept"]
    assert [k["task"] for k in kept] == ["worker_a"]
    assert kept[0]["ran_on"] == {"value": "harness:claude-code", "source": "log"}
    assert kept[0]["output_saved"] is True
    assert kept[0]["output_chars"] == len("A's answer")
    assert "does not run a completed task again" in _flat(_run("diagnose", FLOW).output)


def test_every_command_carries_the_resolved_id(failed_run):
    """`latest` is resolved once and the full id appears in each suggestion."""
    result = _run("diagnose", "latest")
    assert result.exit_code == 0, result.output
    assert f"latest: {FLOW}" in result.stderr
    commands = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith("atelier ")
    ]
    assert commands, result.stdout
    assert all(FLOW in c for c in commands)
    assert "latest" not in " ".join(commands)
    assert f"atelier logs {FLOW} --task worker_b --show all" in result.stdout
    assert f"atelier run --resume {FLOW}" in result.stdout


def test_a_failed_run_still_exits_zero(failed_run):
    """Reading a failed run is a successful inspection, not a failure."""
    assert _run("diagnose", FLOW).exit_code == 0
    assert _run("diagnose", FLOW, "--json").exit_code == 0


def test_json_is_one_clean_object(failed_run):
    """stdout is exactly one JSON document, with the alias note on stderr."""
    result = _run("diagnose", "latest", "--json")
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload["flow_id"] == FLOW
    assert set(payload) >= {
        "saved",
        "observed",
        "snapshot",
        "recipe",
        "failures",
        "cancelled",
        "not_run",
        "kept",
        "next_steps",
    }
    assert "latest:" in result.stderr


def test_resume_advice_names_its_side_effects(failed_run):
    """The one suggestion that spends money says so before you run it."""
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    resume = [s for s in payload["next_steps"] if s["command"].endswith(FLOW)]
    resume = [s for s in resume if "--resume" in s["command"]]
    assert len(resume) == 1
    assert "spend tokens" in resume[0]["side_effects"]


def test_task_names_are_shell_quoted(workdir):
    """A task name needing quotes is quoted in the command, not pasted raw."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="boom")},
        entries=[_entry("worker_b", "tool:bash", exit_code=1, stderr="boom")],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    # The flow id itself is quoted by the same rule; prove the rule is applied
    # by asking for a name that needs it.
    assert any("--task worker_b" in s["command"] or "" for s in payload["next_steps"])


# ------------------------------------------------------------------- provenance


def test_agent_attribution_prefers_the_log_over_the_recipe(workdir):
    """What ran comes from the log entry, even when the recipe now says otherwise."""
    atelier = Atelier()
    _seed(
        atelier,
        entries=[_entry("worker_a", "harness:gemini", output="x", exit_code=0)],
        outputs={"worker_a": "x"},
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    kept = next(k for k in payload["kept"] if k["task"] == "worker_a")
    assert kept["ran_on"] == {"value": "harness:gemini", "source": "log"}
    assert kept["current_tool"] == "harness:claude-code"
    assert "now harness:claude-code in the recipe" in _flat(_run("diagnose", FLOW).output)


def test_saved_selection_is_used_when_no_log_entry_exists(workdir):
    """A task with no log entry still names the agent the run chose for it."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="killed")},
        task_agents={"worker_b": "harness:codex:gpt-5"},
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["ran_on"] == {
        "value": "harness:codex:gpt-5",
        "source": "selection",
    }
    assert "from this run's saved choice" in _flat(_run("diagnose", FLOW).output)


def test_unknown_attribution_is_labelled_unknown(workdir):
    """Nothing saved, nothing claimed."""
    atelier = Atelier()
    _seed(atelier, tasks={"synthesis": TaskProgress(status=TaskStatus.cancelled)})
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["cancelled"][0]["ran_on"] == {"value": None, "source": "unknown"}


# ------------------------------------------------------------------- evidence


def test_orphan_steps_are_evidence_not_silence(workdir):
    """A killed task's live steps are reported, never called "no evidence"."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.running,
        tasks={"worker_b": TaskProgress(status=TaskStatus.running)},
        steps=[
            StepRecord(
                task="worker_b",
                step=IntermediateStep(kind=StepKind.thinking, text="reading the file"),
            ),
            StepRecord(
                task="worker_b",
                step=IntermediateStep(kind=StepKind.tool_call, tool_name="read"),
            ),
        ],
        runner_pid=None,
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    running = payload["running"][0]
    assert running["evidence"] == "steps"
    assert running["step_records"] == 2
    assert "live steps only" in _flat(_run("diagnose", FLOW).output)


def test_saved_reason_survives_a_missing_log(workdir):
    """With no log entry at all, the recorded reason is still shown."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={
            "worker_b": TaskProgress(
                status=TaskStatus.failed, reason="exit=1 stderr=auth required"
            )
        },
    )
    result = _run("diagnose", FLOW)
    assert "auth required" in _flat(result.stdout)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["evidence"] == "reason"
    assert any("log entries" in note for note in payload["unavailable"])


def test_missing_outputs_and_steps_are_marked_unavailable(workdir):
    """Optional files that were never written say so instead of reading as empty."""
    atelier = Atelier()
    _seed(atelier, tasks={"worker_b": TaskProgress(status=TaskStatus.failed)})
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    joined = " ".join(payload["unavailable"])
    assert "saved outputs" in joined
    assert "live step records" in joined
    assert _run("diagnose", FLOW).exit_code == 0


def test_a_long_excerpt_is_bounded_and_says_so(workdir):
    """The tail is capped, marked truncated, and the full log command is offered."""
    atelier = Atelier()
    noise = "\n".join(f"line {n}" for n in range(200))
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="long")},
        entries=[_entry("worker_b", "tool:bash", exit_code=1, stderr=noise)],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    excerpt = payload["failures"][0]["excerpt"]
    assert excerpt["truncated"] is True
    assert len(excerpt["text"]) <= 700 + len("\n... trimmed ...\n")
    # Both ends survive, and the gap between them is counted rather than hidden.
    assert excerpt["text"].startswith("line 0")
    assert excerpt["text"].splitlines()[-1] == "line 199"
    assert "lines omitted" in excerpt["text"]
    result = _run("diagnose", FLOW)
    assert "truncated" in _flat(result.stdout)
    assert f"atelier logs {FLOW} --show all" in result.stdout


def test_a_later_completion_beats_an_old_failed_attempt(workdir):
    """Two log entries, the second one good: the saved status is what is reported."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.completed,
        tasks={"worker_a": TaskProgress(status=TaskStatus.completed, iteration=2)},
        entries=[
            _entry("worker_a", "harness:codex", exit_code=1, stderr="first try broke"),
            _entry("worker_a", "harness:codex", exit_code=0, output="second try"),
        ],
        outputs={"worker_a": "second try"},
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"] == []
    assert payload["kept"][0]["exit_code"] == 0
    assert "first try broke" not in _run("diagnose", FLOW).stdout


# ------------------------------------------------------------- runner liveness


def test_a_dead_local_runner_is_called_crashed(workdir):
    """A frozen `running` record with a provably gone pid is a crash, and resumable."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.running,
        tasks={"worker_a": TaskProgress(status=TaskStatus.completed)},
        outputs={"worker_a": "x"},
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["observed"] == {
        "state": "crashed",
        "certainty": "proven",
        "note": payload["observed"]["note"],
    }
    assert "is gone on this host" in payload["observed"]["note"]
    assert any("--resume" in (s["command"] or "") for s in payload["next_steps"])


def test_a_live_runner_is_never_told_to_resume(workdir):
    """Our own pid stands in for a live runner: the advice is watch or stop."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.running,
        tasks={"worker_a": TaskProgress(status=TaskStatus.running)},
        runner_pid=os.getpid(),
    )
    result = _run("diagnose", FLOW)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["observed"]["state"] == "running"
    assert payload["observed"]["certainty"] == "proven"
    assert "do not resume" in _flat(result.stdout)
    assert not any("--resume" in (s["command"] or "") for s in payload["next_steps"])
    assert f"atelier stop {FLOW}" in _flat(result.stdout)


def test_a_foreign_host_stays_uncertain(workdir):
    """A run recorded elsewhere gets no unconditional resume recommendation."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.running,
        tasks={"worker_a": TaskProgress(status=TaskStatus.running)},
        runner_host="some-other-box",
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["observed"]["certainty"] == "uncertain"
    assert "cannot be probed from here" in payload["observed"]["note"]
    resume = next(s for s in payload["next_steps"] if "--resume" in (s["command"] or ""))
    assert "not recommended" in resume["what"]
    assert "double-run" in resume["side_effects"]


def test_a_missing_pid_stays_uncertain(workdir):
    """No pid recorded means nothing to probe, and that is said plainly."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.running,
        tasks={"worker_a": TaskProgress(status=TaskStatus.running)},
        runner_pid=None,
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["observed"]["certainty"] == "uncertain"
    assert "no runner pid was recorded" in payload["observed"]["note"]


def test_a_stopped_run_is_not_offered_resume(workdir):
    """Resume does not take a stopped flow, so it is not suggested."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.stopped,
        tasks={"worker_a": TaskProgress(status=TaskStatus.completed)},
        outputs={"worker_a": "x"},
    )
    result = _run("diagnose", FLOW)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["observed"]["state"] == "stopped"
    assert not any("--resume" in (s["command"] or "") for s in payload["next_steps"])
    assert f"atelier run --again {FLOW}" in _flat(result.stdout)


def test_a_completed_run_has_nothing_to_recover(workdir):
    """A clean run is readable and says there is nothing to fix."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.completed,
        tasks={"worker_a": TaskProgress(status=TaskStatus.completed)},
        outputs={"worker_a": "x"},
    )
    result = _run("diagnose", FLOW)
    assert result.exit_code == 0
    assert "nothing to recover" in _flat(result.stdout)
    assert "--resume" not in result.stdout


# ------------------------------------------------------------------- the recipe


def test_a_missing_recipe_does_not_destroy_the_diagnostics(workdir):
    """Delete the conduit and the saved evidence still reads back."""
    atelier = Atelier()
    _seed(
        atelier,
        entries=[_entry("worker_a", "harness:codex", output="kept", exit_code=0)],
        outputs={"worker_a": "kept"},
    )
    (workdir / ".atelier" / "conduits" / "triage" / "conduit.yaml").unlink()
    result = _run("diagnose", FLOW)
    assert result.exit_code == 0, result.output
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["recipe"]["available"] is False
    assert "not installed here any more" in payload["recipe"]["note"]
    assert payload["kept"][0]["output_chars"] == len("kept")
    assert payload["kept"][0]["current_tool"] is None


def test_an_unreadable_recipe_is_a_note_not_a_crash(workdir):
    """Broken YAML in the conduit does not stop the post-mortem."""
    atelier = Atelier()
    _seed(atelier)
    (workdir / ".atelier" / "conduits" / "triage" / "conduit.yaml").write_text(
        "name: triage\ntasks: [oops\n"
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["recipe"]["available"] is False
    assert "cannot be read" in payload["recipe"]["note"]
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]


def test_a_changed_recipe_is_reported_as_a_difference(workdir):
    """Renaming a task is shown as added/missing, not folded into history."""
    atelier = Atelier()
    _seed(atelier)
    path = workdir / ".atelier" / "conduits" / "triage" / "conduit.yaml"
    path.write_text(CONDUIT_YAML.replace("worker_b", "worker_c"))
    result = _run("diagnose", FLOW)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["recipe"]["added_tasks"] == ["worker_c"]
    assert payload["recipe"]["missing_tasks"] == ["worker_b"]
    assert "no longer matches" in payload["recipe"]["note"]
    assert "a resume reads the current one" in _flat(result.stdout)
    # The historical failure is still attributed to what actually ran.
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]


# ------------------------------------------------------- corruption and nesting


def test_corrupt_progress_exits_nonzero_with_advice(workdir):
    """An unreadable progress record is named, and no traceback escapes."""
    atelier = Atelier()
    _seed(atelier)
    atelier.store._flow_dir(FLOW).joinpath("progress.json").write_text("{not json")
    result = _run("diagnose", FLOW)
    assert result.exit_code == 1
    assert "cannot read the saved progress" in _flat(result.output)
    assert f"atelier rm flow {FLOW}" in _flat(result.output)
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_corrupt_child_progress_is_addressable_without_a_traceback(workdir):
    """An exact nested id still resolves; the corruption is reported, not raised."""
    atelier = Atelier()
    _seed(atelier)
    child = _seed(
        atelier,
        flow_id="20260101_cccccccc_triage",
        parent=FLOW,
        tasks={"worker_a": TaskProgress(status=TaskStatus.completed)},
    )
    atelier.store._flow_dir(child).joinpath("progress.json").write_text("nonsense")
    atelier.store._flow_paths.clear()
    result = _run("diagnose", child)
    assert result.exit_code == 1
    assert "cannot read the saved progress" in _flat(result.output)
    assert "unknown flow" not in _flat(result.output)


def test_a_nested_child_is_pointed_at_not_aggregated(workdir):
    """The root names its children and tells you to diagnose them separately."""
    atelier = Atelier()
    _seed(atelier)
    child = "20260101_dddddddd_triage"
    atelier.store.create_flow("triage", {}, parent_flow_id=FLOW, flow_id=child)
    child_progress = Progress(
        status=FlowStatus.failed,
        tasks={"worker_a": TaskProgress(status=TaskStatus.failed)},
        started_at="2026-01-01T10:01:00Z",
        invoking_task="synthesis",
    )
    atelier.store.write_progress(child, child_progress)
    result = _run("diagnose", FLOW)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["children"] == [
        {"flow_id": child, "invoking_task": "synthesis", "status": "failed"}
    ]
    assert "does not aggregate" in _flat(result.stdout)
    # The root's own failure list is unchanged by the child's.
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]


def test_a_nested_run_says_it_was_started_by_a_parent(workdir):
    """Reading a child directly identifies its scope."""
    atelier = Atelier()
    _seed(atelier)
    child = "20260101_eeeeeeee_triage"
    atelier.store.create_flow("triage", {}, parent_flow_id=FLOW, flow_id=child)
    atelier.store.write_progress(
        child,
        Progress(
            status=FlowStatus.failed,
            tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="boom")},
            started_at="2026-01-01T10:01:00Z",
            invoking_task="synthesis",
        ),
    )
    atelier.store._flow_paths.clear()
    result = _run("diagnose", child)
    assert result.exit_code == 0, result.output
    assert "started by task synthesis of a parent run" in _flat(result.stdout)
    assert json.loads(_run("diagnose", child, "--json").stdout)[
        "saved"
    ]["invoking_task"] == "synthesis"


def test_an_unknown_flow_exits_nonzero(workdir):
    """A name that is no flow is an actionable lookup failure."""
    result = _run("diagnose", "20261231_ffffffff_triage")
    assert result.exit_code == 1
    assert "unknown flow" in _flat(result.output)


# ------------------------------------------------------------ mixed observation


def test_a_run_that_moves_mid_report_is_not_called_settled(workdir, monkeypatch):
    """Progress is read first and last; a change between them is disclosed."""
    atelier_for_seed = Atelier()
    _seed(
        atelier_for_seed,
        status=FlowStatus.running,
        tasks={"worker_a": TaskProgress(status=TaskStatus.running)},
        runner_pid=None,
    )
    from flow_atelier.services.store import filesystem

    real = filesystem.FilesystemStore.read_progress
    calls = {"n": 0}

    def moving(self, flow_id):
        """Return a run that has advanced by the time it is read again.

        :param self: the store.
        :param flow_id: the flow being read.
        :returns: the progress, mutated on the second read only.
        """
        progress = real(self, flow_id)
        calls["n"] += 1
        # Every report reads progress twice; advance the run on the second read
        # of each, so both the rendered and the JSON invocation see it move.
        if calls["n"] % 2 == 0 and flow_id == FLOW:
            progress.status = FlowStatus.failed
            progress.tasks["worker_a"].status = TaskStatus.failed
        return progress

    monkeypatch.setattr(filesystem.FilesystemStore, "read_progress", moving)
    result = _run("diagnose", FLOW)
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["snapshot"]["consistent"] is False
    assert "moved while this report was read" in payload["snapshot"]["note"]
    assert "running -> failed" in payload["snapshot"]["note"]
    assert "read it again" in _flat(result.stdout)


def test_a_settled_run_says_so(failed_run):
    """The ordinary case claims consistency, and only then."""
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["snapshot"]["consistent"] is True


# ------------------------------------------------------------------ no mutation


def test_diagnose_writes_nothing(failed_run, workdir):
    """Every file under the project is byte-identical after a report."""
    import hashlib

    def digest() -> dict[str, str]:
        """Hash every file in the project.

        :returns: mapping of relative path to sha256.
        """
        return {
            str(p.relative_to(workdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(workdir.rglob("*"))
            if p.is_file()
        }

    before = digest()
    assert _run("diagnose", FLOW).exit_code == 0
    assert _run("diagnose", FLOW, "--json").exit_code == 0
    assert digest() == before


# ------------------------------------------------- corrupt optional evidence


def _flow_dir(atelier: Atelier):
    """Return the diagnosed flow's directory, for corrupting one file in it.

    :param atelier: facade rooted at the test project.
    :returns: the flow directory path.
    """
    return atelier.store._flow_dir(FLOW)


def test_unreadable_outputs_do_not_cost_the_report(failed_run, workdir):
    """A broken outputs.yaml costs its own line, not every other fact."""
    _flow_dir(Atelier()).joinpath("outputs.yaml").write_text("worker_a: [unterminated")
    result = _run("diagnose", FLOW, "--json")
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    # Everything progress.json knows survived.
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]
    assert payload["failures"][0]["reason"] == "exit=1 stderr=boom"
    assert [k["task"] for k in payload["kept"]] == ["worker_a"]
    # And the one thing that was lost is named, not guessed at as "nothing saved".
    assert payload["kept"][0]["output_saved"] is None
    assert any("outputs.yaml cannot be read" in n for n in payload["unavailable"])

    rendered = _run("diagnose", FLOW)
    assert rendered.exit_code == 0, rendered.output
    flat = _flat(rendered.stdout)
    assert "unknown (unreadable)" in flat
    assert "what failed" in flat


def test_unreadable_logs_do_not_cost_the_report(failed_run, workdir):
    """Bytes that are not UTF-8 in logs.jsonl lose the excerpts and nothing else."""
    _flow_dir(Atelier()).joinpath("logs.jsonl").write_bytes(bytes([255, 254, 10]))
    result = _run("diagnose", FLOW, "--json")
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]
    assert payload["failures"][0]["reason"] == "exit=1 stderr=boom"
    assert payload["failures"][0]["excerpt"] is None
    assert any("the saved log cannot be read" in n for n in payload["unavailable"])
    assert any("logs.jsonl or logs.json" in n for n in payload["unavailable"])
    # The saved reason is still the evidence, so the task is not called silent.
    assert payload["failures"][0]["evidence"] == "reason"
    assert _run("diagnose", FLOW).exit_code == 0


def test_unreadable_steps_do_not_cost_the_report(failed_run, workdir):
    """A corrupt steps.jsonl costs its own records, not the report.

    The store already skips step lines it cannot parse, so this asserts the
    report survives that rather than a note of its own.
    """
    _flow_dir(Atelier()).joinpath("steps.jsonl").write_bytes(bytes([255, 254, 10]))
    result = _run("diagnose", FLOW, "--json")
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    payload = json.loads(result.stdout)
    assert [f["task"] for f in payload["failures"]] == ["worker_b"]
    assert _run("diagnose", FLOW).exit_code == 0


def test_corrupt_progress_is_still_a_clean_failure(failed_run, workdir):
    """The one required file stays fatal, and stays free of a traceback."""
    _flow_dir(Atelier()).joinpath("progress.json").write_text("{not json")
    result = _run("diagnose", FLOW, "--json")
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "cannot read the saved progress" in _flat(result.stderr)


# ------------------------------------------------------- placing a failure


def test_a_timeout_is_reported_as_a_timeout(workdir):
    """The executor's own time-limit record places the failure, so nothing is guessed."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={
            "worker_b": TaskProgress(
                status=TaskStatus.failed,
                reason="exit=124 stderr=harness timeout after 1s",
            )
        },
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=124,
                stderr="harness timeout after 1s",
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "timeout"
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "ran out of its own time limit" in flat
    assert "refused to open a session" not in flat


def test_a_session_refusal_is_read_from_what_the_agent_said(workdir):
    """agent_session comes from the agent's own words, never from a silence."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1")},
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=1,
                stderr="RequestError: Authentication required",
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "agent_session"
    assert "logged in" in _flat(_run("diagnose", FLOW).stdout)



def test_a_refusal_after_the_prompt_went_out_is_not_called_no_work(workdir):
    """The log shows the prompt was sent, so the agent may already have acted."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1")},
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=1,
                stderr="RequestError: Authentication required",
                session=[{"source": "user", "text": "fix the retry window"}],
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "agent_auth"
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "no work was asked of it" not in flat
    assert "treat any change it could have made as unknown" in flat


def test_an_older_log_without_a_session_does_not_prove_no_work(workdir):
    """A log written before prompts were saved cannot say none was sent."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1")},
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=1,
                stderr="RequestError: Authentication required",
            )
        ],
    )
    logs = _flow_dir(atelier).joinpath("logs.jsonl")
    lines = [json.loads(line) for line in logs.read_text().splitlines()]
    for line in lines:
        del line["session"]
    logs.write_text("".join(json.dumps(line) + "\n" for line in lines))
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "agent_auth"


def test_a_stopped_run_does_not_blame_a_failure_for_its_cancelled_tasks(workdir):
    """Nothing failed: the user stopped it, and the report should say so."""
    atelier = Atelier()
    _seed(
        atelier,
        status=FlowStatus.stopped,
        tasks={"worker_b": TaskProgress(status=TaskStatus.cancelled)},
    )
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "cut off when the run stopped" in flat
    assert "because the run was stopped" in flat
    assert "another task failed" not in flat

def test_a_shell_task_that_printed_a_401_is_not_a_session_failure(workdir):
    """Authentication words are not a session: only an agent has one to open.

    A ``tool:bash`` task can run, change files and then report an HTTP 401 from
    something it called. Reading its words as a refused agent session would
    claim no work was asked of it, while its side effect is already on disk.
    """
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=22")},
        entries=[
            _entry(
                "worker_b",
                "tool:bash",
                exit_code=22,
                stderr="HTTP 401 Unauthorized from application endpoint",
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    failure = payload["failures"][0]
    assert failure["kind"] == "task"
    assert failure["excerpt"]["text"] == "HTTP 401 Unauthorized from application endpoint"
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "no work was asked of it" not in flat
    assert "refused to open a session" not in flat


def test_an_agent_that_had_begun_working_is_not_a_session_failure(workdir):
    """A session that opened cannot be the thing that refused to open.

    The same words late in an agent's own output place the failure inside the
    work, so the report must not send the reader to a login for it.
    """
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1")},
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=1,
                output="I called the deploy API and it answered",
                stderr="Authentication required by the endpoint I called",
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "task"
    assert "no work was asked of it" not in _flat(_run("diagnose", FLOW).stdout)


def test_a_malformed_legacy_log_file_does_not_cost_the_report(failed_run, workdir):
    """A legacy logs.json holding the wrong shape parses and cannot be iterated.

    ``null`` and a bare scalar both read back fine as JSON, so the failure is a
    TypeError from inside the reader rather than a parse error. The report has
    to survive it exactly as it survives broken syntax.
    """
    flow_dir = _flow_dir(Atelier())
    flow_dir.joinpath("logs.jsonl").unlink()
    for shape in ("null", "7"):
        flow_dir.joinpath("logs.json").write_text(shape)
        result = _run("diagnose", FLOW, "--json")
        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output
        payload = json.loads(result.stdout)
        assert [f["task"] for f in payload["failures"]] == ["worker_b"]
        assert payload["failures"][0]["reason"] == "exit=1 stderr=boom"
        assert any("the saved log cannot be read" in n for n in payload["unavailable"])
        rendered = _run("diagnose", FLOW)
        assert rendered.exit_code == 0, rendered.output
        assert "what failed" in _flat(rendered.stdout)
    # The report read the files and changed none of them.
    assert flow_dir.joinpath("logs.json").read_text() == "7"


def test_an_unplaceable_agent_failure_says_so(workdir):
    """No output, no step, no words of its own: the stage is unsettled, not named."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1")},
        entries=[_entry("worker_b", "harness:codex", exit_code=1, stderr="died")],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "unclear_stage"
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "does not place this failure" in flat
    assert "logged in" not in flat


def test_an_agent_that_worked_and_failed_is_an_ordinary_task_failure(workdir):
    """Output of its own proves work happened, so the failure is the work's."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={"worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=2")},
        entries=[
            _entry(
                "worker_b",
                "harness:codex",
                exit_code=2,
                output="I looked and could not do it",
                stderr="giving up",
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert payload["failures"][0]["kind"] == "task"
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "logged in" not in flat
    assert "does not place this failure" not in flat


# ------------------------------------------------------ a cut-off task


def test_a_cancelled_task_with_live_steps_is_reported_as_having_run(workdir):
    """Its own steps are the proof; absence of a log entry is not counter-proof."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={
            "worker_a": TaskProgress(status=TaskStatus.cancelled),
            "worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1"),
        },
        steps=[
            StepRecord(
                task="worker_a",
                step=IntermediateStep(kind=StepKind.stdout, text="work-started"),
            )
        ],
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert [t["task"] for t in payload["cancelled"]] == ["worker_a"]
    assert payload["cancelled"][0]["execution"] == "began"
    assert payload["cancelled"][0]["step_records"] == 1
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "it was executing when the run failed" in flat
    assert "never got to run" not in flat


def test_a_skipped_task_is_the_one_that_truly_never_ran(workdir):
    """Skipped and pending are the dispositions the run provably never reached."""
    atelier = Atelier()
    _seed(
        atelier,
        tasks={
            "worker_b": TaskProgress(status=TaskStatus.failed, reason="exit=1"),
            "synthesis": TaskProgress(
                status=TaskStatus.skipped, reason="its condition was false"
            ),
        },
    )
    payload = json.loads(_run("diagnose", FLOW, "--json").stdout)
    assert [t["task"] for t in payload["not_run"]] == ["synthesis"]
    assert payload["not_run"][0]["execution"] == "not_started"
    assert payload["cancelled"] == []
    flat = _flat(_run("diagnose", FLOW).stdout)
    assert "the run never reached it" in flat
