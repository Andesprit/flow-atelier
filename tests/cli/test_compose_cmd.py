"""CLI tests for `atelier compose` — build a multi-agent conduit from arguments."""
from __future__ import annotations

import json
import os
import sys

import pytest
import yaml
from typer.testing import CliRunner

from flow_atelier.cli import app
from flow_atelier.modules.engine import validate_conduit
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.services.store.filesystem import FilesystemStore

MISSING = "definitely-not-on-path-atelier"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Isolated cwd with an empty `.atelier` tree and isolated global dir.

    Two custom harnesses are registered so the tests never depend on a real
    agent: `alpha` and `beta` point at this interpreter, so they count as
    installed, and `ghost` points at a command that does not exist.

    :param tmp_path: pytest temp directory fixture.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: the workspace path.
    """
    (tmp_path / ".atelier" / "conduits").mkdir(parents=True)
    global_dir = tmp_path / "global"
    (global_dir / "conduits").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("ATELIER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATELIER_GLOBAL_ATELIER_DIR", str(global_dir))
    monkeypatch.setenv("ATELIER_NO_UPDATE_CHECK", "1")
    monkeypatch.setenv(
        "ATELIER_HARNESSES",
        json.dumps(
            {
                "alpha": [sys.executable, "-c", "pass"],
                "beta": [sys.executable, "-c", "pass"],
                "ghost": [MISSING, "--acp"],
            }
        ),
    )
    return tmp_path


def _conduit(workdir, name: str) -> Conduit:
    """Load a composed conduit straight off disk.

    :param workdir: the workspace path.
    :param name: the conduit name.
    :returns: the parsed conduit.
    """
    path = workdir / ".atelier" / "conduits" / name / "conduit.yaml"
    return Conduit.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _compose(*args: str):
    """Invoke `atelier compose` with the given arguments.

    :param args: arguments after the subcommand.
    :returns: the CliRunner result.
    """
    return CliRunner().invoke(app, ["compose", *args])


def test_sequential_chains_brief_and_previous_result(workdir):
    """The default shape hands the brief and each result down the chain."""
    result = _compose(
        "chain", "-s", "alpha=Draft it", "-s", "beta=Review it", "-s", "alpha=Ship it"
    )
    assert result.exit_code == 0, result.output
    conduit = _conduit(workdir, "chain")
    validate_conduit(conduit)

    assert [t.name for t in conduit.tasks] == ["step_1", "step_2", "step_3"]
    assert [t.tool for t in conduit.tasks] == [
        "harness:alpha", "harness:beta", "harness:alpha"
    ]
    assert [t.depends_on for t in conduit.tasks] == [[], ["step_1"], ["step_2"]]
    assert set(conduit.inputs) == {"brief"}
    assert conduit.inputs["brief"].default is None

    first, second, third = conduit.tasks
    assert first.task.startswith("Draft it\n")
    assert "{{inputs.brief}}" in first.task
    assert "{{step_1.output}}" not in first.task
    # Each later step sees the brief and exactly its predecessor, attributed.
    assert "{{inputs.brief}}" in second.task
    # The agent is a reference, not a literal: a run may re-point step_1.
    assert "RESULT FROM step_1 ({{step_1.tool}})" in second.task
    assert "{{step_1.output}}" in second.task
    assert "{{step_2.output}}" not in second.task
    assert "{{step_2.output}}" in third.task
    assert "{{step_1.output}}" not in third.task


def test_parallel_workers_share_only_the_brief(workdir):
    """`--parallel` removes the links and lets every worker run at once."""
    assert _compose(
        "panel", "--parallel", "-s", "alpha=Correctness", "-s", "beta=Security"
    ).exit_code == 0
    conduit = _conduit(workdir, "panel")
    validate_conduit(conduit)
    assert [t.depends_on for t in conduit.tasks] == [[], []]
    assert conduit.max_concurrency == 2
    for task in conduit.tasks:
        assert "{{inputs.brief}}" in task.task
        assert ".output}}" not in task.task


def test_synthesis_waits_for_every_attributed_worker(workdir):
    """`--synthesize` adds one final task fed by all of them."""
    assert _compose(
        "panel", "--parallel",
        "-s", "alpha=Correctness", "-s", "beta=Security",
        "--synthesize", "alpha=Merge the findings",
    ).exit_code == 0
    conduit = _conduit(workdir, "panel")
    validate_conduit(conduit)
    synthesis = conduit.tasks[-1]
    assert synthesis.name == "synthesis"
    assert synthesis.depends_on == ["step_1", "step_2"]
    assert synthesis.task.startswith("Merge the findings\n")
    for marker in (
        "RESULT FROM step_1 ({{step_1.tool}})",
        "RESULT FROM step_2 ({{step_2.tool}})",
        "{{step_1.output}}",
        "{{step_2.output}}",
        "{{inputs.brief}}",
    ):
        assert marker in synthesis.task


def test_model_and_effort_suffixes_survive(workdir):
    """A harness may carry `:model` and `:model:effort`, prefixed or not."""
    assert _compose(
        "tuned",
        "-s", "alpha:opus[1m]=Draft",
        "-s", "harness:beta:gpt-5.1:high=Review",
    ).exit_code == 0
    conduit = _conduit(workdir, "tuned")
    assert [t.tool for t in conduit.tasks] == [
        "harness:alpha:opus[1m]", "harness:beta:gpt-5.1:high"
    ]


def test_prompts_round_trip_text_yaml_could_mangle(workdir):
    """Multiline, Unicode, quotes, braces and equals signs survive the file."""
    prompt = (
        'Emit {"a": 1} and keep "quotes", \'single\' and “curly” — ñ ✓\n'
        "  indented: key=value=more\n"
        "\ttab and 2>&1 and $HOME and #hash"
    )
    assert _compose("rt", "-s", f"alpha={prompt}", "-s", "beta=Second").exit_code == 0
    conduit = _conduit(workdir, "rt")
    assert conduit.tasks[0].task.startswith(prompt + "\n")
    # Nothing in that text became a template reference the engine would act on.
    validate_conduit(conduit)


def test_the_prompt_is_trimmed_only_at_its_edges(workdir):
    """The documented normalization: outer whitespace goes, inner stays."""
    assert _compose(
        "trim", "-s", "alpha=  \n first\n\n  second  \n ", "-s", "beta=x"
    ).exit_code == 0
    assert _conduit(workdir, "trim").tasks[0].task.startswith(
        "first\n\n  second\n"
    )


def test_only_the_first_equals_separates(workdir):
    """A prompt keeps every equals sign after the first."""
    assert _compose(
        "eq", "-s", "alpha=set a=1 and b=2", "-s", "beta=x"
    ).exit_code == 0
    assert _conduit(workdir, "eq").tasks[0].task.startswith("set a=1 and b=2\n")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-s", "alpha=only one"], "at least two"),
        (["-s", "alpha", "-s", "beta=x"], "expected HARNESS=PROMPT"),
        (["-s", "alpha=", "-s", "beta=x"], "expected HARNESS=PROMPT"),
        (["-s", "=prompt", "-s", "beta=x"], "expected HARNESS=PROMPT"),
        (["-s", "Alpha Agent=x", "-s", "beta=y"], "is not a harness name"),
        (["-s", "tool:bash=x", "-s", "beta=y"], "not usable here"),
        (
            ["-s", "alpha=x", "-s", "beta=y", "--synthesize", "alpha=z"],
            "--synthesize needs --parallel",
        ),
        (
            ["-s", "alpha=use {{inputs.other}}", "-s", "beta=y"],
            "remove {{inputs.other}}",
        ),
        (["-s", "alpha=use {{step_9.output}}", "-s", "beta=y"], "remove "),
    ],
)
def test_misuse_exits_two_without_writing(workdir, args, expected):
    """Every option mistake is refused with its own reason and writes nothing."""
    result = _compose("nope", *args)
    assert result.exit_code == 2, result.output
    assert expected in result.output.replace("\n", " ").replace("  ", " ")
    assert "Traceback" not in result.output
    assert not (workdir / ".atelier" / "conduits" / "nope").exists()


def test_invalid_name_is_refused_in_plain_words(workdir):
    """A bad conduit name fails before pydantic's field report reaches a user."""
    result = _compose("bad name!", "-s", "alpha=x", "-s", "beta=y")
    assert result.exit_code == 2
    assert "invalid conduit name" in result.output
    assert "pydantic" not in result.output


def test_unknown_harness_is_refused(workdir):
    """A harness nothing is registered for fails, and nothing is written."""
    result = _compose("nope", "-s", "nosuchagent=x", "-s", "beta=y")
    assert result.exit_code == 1
    assert "unknown harness" in result.output
    assert not (workdir / ".atelier" / "conduits" / "nope").exists()


def test_known_but_uninstalled_harness_is_written_with_guidance(workdir):
    """A registered agent you have not installed is authored, with a warning."""
    result = _compose("later", "-s", "ghost=x", "-s", "beta=y")
    assert result.exit_code == 0, result.output
    assert "not installed yet" in result.output
    assert MISSING in result.output
    assert (workdir / ".atelier" / "conduits" / "later" / "conduit.yaml").exists()


def test_compose_refuses_to_clobber_a_project_conduit(workdir):
    """A second compose under the same name leaves the first file untouched."""
    assert _compose("dup", "-s", "alpha=first", "-s", "beta=x").exit_code == 0
    path = workdir / ".atelier" / "conduits" / "dup" / "conduit.yaml"
    before = path.read_text(encoding="utf-8")
    result = _compose("dup", "-s", "alpha=second", "-s", "beta=y")
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert path.read_text(encoding="utf-8") == before


def test_compose_never_truncates_a_conduit_that_lands_after_the_lookup(
    workdir, monkeypatch
):
    """A competitor that wins the race keeps its bytes; the loser fails."""
    path = workdir / ".atelier" / "conduits" / "race" / "conduit.yaml"
    rival = "name: race\n# finished by the other composer\n"
    original = FilesystemStore.conduit_source

    def lookup_then_lose_the_race(self, name: str):
        """Answer as usual, then let a competing writer finish the file."""
        try:
            return original(self, name)
        finally:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rival, encoding="utf-8")

    monkeypatch.setattr(FilesystemStore, "conduit_source", lookup_then_lose_the_race)
    result = _compose("race", "-s", "alpha=x", "-s", "beta=y")
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert path.read_text(encoding="utf-8") == rival


def test_compose_refuses_to_shadow_a_global_conduit(workdir):
    """A name already served globally is refused rather than shadowed."""
    global_conduit = workdir / "global" / "conduits" / "shared"
    global_conduit.mkdir(parents=True)
    (global_conduit / "conduit.yaml").write_text("name: shared\n")
    result = _compose("shared", "-s", "alpha=x", "-s", "beta=y")
    assert result.exit_code == 1
    assert "already exists" in result.output and "global" in result.output
    assert not (workdir / ".atelier" / "conduits" / "shared").exists()


def test_compose_starts_nothing(workdir):
    """Composition is inert: a conduit appears, no flow does."""
    assert _compose("inert", "-s", "alpha=x", "-s", "beta=y").exit_code == 0
    assert list((workdir / ".atelier").glob("flows/*")) == []


def test_composed_conduit_lists_checks_and_plans(workdir):
    """The result is an ordinary conduit the existing commands consume."""
    assert _compose(
        "wired", "--parallel", "-s", "alpha=x", "-s", "beta=y",
        "--synthesize", "alpha=z",
    ).exit_code == 0
    runner = CliRunner()
    assert "wired" in runner.invoke(app, ["list", "conduits"]).output
    checked = runner.invoke(app, ["check", "wired", "--json"])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)[0]["required_inputs"] == ["brief"]
    planned = runner.invoke(app, ["plan", "wired", "--json"])
    assert planned.exit_code == 0, planned.output
    waves = json.loads(planned.stdout)["waves"]
    assert [sorted(t["name"] for t in w) for w in waves] == [
        ["step_1", "step_2"], ["synthesis"]
    ]


def test_next_steps_name_the_commands_that_follow(workdir):
    """The printed guidance leads to a check, a run with the brief, and output."""
    result = _compose(
        "guided", "--parallel", "-s", "alpha=x", "-s", "beta=y",
        "--synthesize", "alpha=z",
    )
    for expected in (
        "atelier check guided",
        "atelier plan guided",
        "atelier run guided --input brief=",
        "atelier outputs latest --task synthesis",
    ):
        assert expected in result.output
