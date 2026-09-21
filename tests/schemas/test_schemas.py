"""Schema unit tests."""
import re

import pytest
import yaml
from pydantic import ValidationError

from flow_atelier.schemas.conduit import Conduit, ToolType
from flow_atelier.schemas.flow import FLOW_ID_RE, new_flow_id, parse_flow_id
from flow_atelier.schemas.log import LogEntry
from flow_atelier.schemas.progress import FlowStatus, Progress, TaskProgress, TaskStatus

SAMPLE_YAML = """
name: deploy_pipeline
description: Build test deploy
timeout: 600
max_concurrency: 4
inputs:
  repo_url: repo URL
  branch: branch name
tasks:
  - clone_repo:
      description: Clone
      task: "git clone {{inputs.repo_url}}"
      tool: tool:bash
      depends_on: []
  - run_tests:
      description: Test
      task: "make test"
      tool: tool:bash
      depends_on: [clone_repo]
      repeat: 3
  - review:
      description: Review
      task: "review the code"
      tool: harness:claude-code
      depends_on: [clone_repo]
      interactive: false
  - deploy:
      description: Deploy
      task: "make deploy"
      tool: tool:bash
      depends_on:
        - run_tests
        - review.output.match(VERDICT:\\s*APPROVE)
  - approve:
      description: human gate
      task: "Please answer"
      tool: tool:hitl
      depends_on: [run_tests]
      inputs:
        confirm: "Type yes to confirm"
"""


def test_parse_sample_conduit():
    """Verify the sample YAML parses into a Conduit with expected fields."""
    data = yaml.safe_load(SAMPLE_YAML)
    conduit = Conduit.model_validate(data)
    assert conduit.name == "deploy_pipeline"
    assert conduit.timeout == 600
    assert conduit.max_concurrency == 4
    assert len(conduit.tasks) == 5
    names = [t.name for t in conduit.tasks]
    assert names == ["clone_repo", "run_tests", "review", "deploy", "approve"]
    run_tests = conduit.tasks[1]
    assert run_tests.repeat == 3
    assert run_tests.tool == ToolType.bash
    review = conduit.tasks[2]
    assert review.tool == ToolType.claude
    deploy = conduit.tasks[3]
    assert "review.output.match(VERDICT:\\s*APPROVE)" in deploy.depends_on
    approve = conduit.tasks[4]
    assert approve.inputs == {"confirm": "Type yes to confirm"}


def test_defaults():
    """Verify default Conduit and task fields when not provided."""
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "tasks": [
                {"t": {"description": "d", "task": "echo hi", "tool": "tool:bash", "depends_on": []}}
            ],
        }
    )
    assert c.timeout == 3600
    assert c.max_concurrency == 3
    assert c.tasks[0].repeat == 1
    assert c.tasks[0].interactive is False


def test_inputs_accept_string_and_object_forms():
    """Verify plain-string inputs and {description, default} objects both parse."""
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "inputs": {
                "plain": "just a description",
                "rich": {"description": "with default", "default": "dv"},
            },
            "tasks": [
                {"t": {"description": "d", "task": "echo hi", "tool": "tool:bash", "depends_on": []}}
            ],
        }
    )
    assert c.inputs["plain"].description == "just a description"
    assert c.inputs["plain"].default is None
    assert c.inputs["rich"].description == "with default"
    assert c.inputs["rich"].default == "dv"


def test_conduit_rejects_nonpositive_timeout():
    """Verify timeout values below 1 fail validation."""
    for bad in (0, -5):
        with pytest.raises(Exception, match="timeout"):
            Conduit.model_validate(
                {
                    "name": "x",
                    "description": "d",
                    "timeout": bad,
                    "tasks": [
                        {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                               "depends_on": []}}
                    ],
                }
            )


def test_conduit_rejects_nonpositive_max_concurrency():
    """Verify max_concurrency values below 1 fail validation."""
    for bad in (0, -1):
        with pytest.raises(Exception, match="max_concurrency"):
            Conduit.model_validate(
                {
                    "name": "x",
                    "description": "d",
                    "max_concurrency": bad,
                    "tasks": [
                        {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                               "depends_on": []}}
                    ],
                }
            )


def test_duplicate_task_names_rejected():
    """Verify duplicate task names cause Conduit validation to fail."""
    with pytest.raises(Exception):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {"a": {"description": "d", "task": "echo 1", "tool": "tool:bash", "depends_on": []}},
                    {"a": {"description": "d", "task": "echo 2", "tool": "tool:bash", "depends_on": []}},
                ],
            }
        )


def test_on_exhaust_requires_loop_predicate():
    """Verify on_exhaust: fail is rejected without until/while."""
    with pytest.raises(Exception, match="on_exhaust requires until or while"):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                           "depends_on": [], "repeat": 3, "on_exhaust": "fail"}}
                ],
            }
        )


def test_on_exhaust_fail_with_until_accepted():
    """Verify on_exhaust: fail validates alongside an until predicate."""
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "tasks": [
                {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                       "depends_on": [], "repeat": 3,
                       "until": "output.match(ok)", "on_exhaust": "fail"}}
            ],
        }
    )
    assert c.tasks[0].on_exhaust == "fail"


@pytest.mark.parametrize(
    ("repeat", "limit", "msg"),
    [
        (1, 2, "stagnation_limit requires repeat > 1"),
        (3, 1, "stagnation_limit must be >= 2"),
    ],
)
def test_stagnation_limit_validation(repeat, limit, msg):
    """Verify stagnation_limit constraints are enforced at load time.

    :param repeat: parametrized repeat value.
    :param limit: parametrized stagnation_limit value.
    :param msg: expected validation error fragment.
    """
    with pytest.raises(Exception, match=msg):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                           "depends_on": [], "repeat": repeat,
                           "stagnation_limit": limit}}
                ],
            }
        )


def test_log_entry_last_turn_output_optional():
    """Verify last_turn_output round-trips and defaults to None for legacy entries."""
    base = {
        "task": "t", "tool": "tool:bash",
        "started_at": "2026-06-11T00:00:00Z", "finished_at": "2026-06-11T00:00:01Z",
    }
    legacy = LogEntry.model_validate(base)
    assert legacy.last_turn_output is None
    entry = LogEntry.model_validate({**base, "last_turn_output": "tail"})
    assert LogEntry.model_validate(entry.model_dump()).last_turn_output == "tail"


def test_log_entry_usage_optional_and_round_trips():
    """Verify usage defaults to None for legacy entries and round-trips."""
    base = {
        "task": "t", "tool": "tool:bash",
        "started_at": "2026-06-11T00:00:00Z", "finished_at": "2026-06-11T00:00:01Z",
    }
    legacy = LogEntry.model_validate(base)
    assert legacy.usage is None
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cost": 0.01}
    entry = LogEntry.model_validate({**base, "usage": usage})
    reparsed = LogEntry.model_validate(entry.model_dump(mode="json"))
    assert reparsed.usage is not None
    assert reparsed.usage.total_tokens == 120
    assert reparsed.usage.cost == 0.01


@pytest.mark.parametrize("bad_name", ["my-task", "a.b", "a b", ""])
def test_invalid_task_names_rejected(bad_name):
    """Verify task names outside [A-Za-z0-9_]+ are rejected at load time.

    :param bad_name: parametrized invalid task name under test.
    """
    with pytest.raises(Exception, match="invalid task name"):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {
                        "name": bad_name,
                        "description": "d",
                        "task": "echo 1",
                        "tool": "tool:bash",
                        "depends_on": [],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "bad_name", ["../evil", "a/b", "..", ".", "a.b", "a b", ""]
)
def test_invalid_conduit_names_rejected(bad_name):
    """Verify conduit names that escape a single path component are rejected.

    :param bad_name: parametrized unsafe conduit name under test.
    """
    with pytest.raises(Exception, match="invalid conduit name"):
        Conduit.model_validate(
            {"name": bad_name, "description": "d", "tasks": []}
        )


@pytest.mark.parametrize("good_name", ["autonomous-projects", "x", "a_b-c1"])
def test_hyphenated_conduit_names_allowed(good_name):
    """Verify real dash-named conduits still validate.

    :param good_name: parametrized safe conduit name under test.
    """
    conduit = Conduit.model_validate(
        {"name": good_name, "description": "d", "tasks": []}
    )
    assert conduit.name == good_name


@pytest.mark.parametrize(
    "tool_str",
    ["harness:opencode", "harness:copilot", "harness:cursor"],
)
def test_new_harness_tool_strings_validate(tool_str):
    """Verify new harness tool identifiers validate on Conduit tasks.

    :param tool_str: parametrized tool identifier string under test.
    """
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "tasks": [
                {
                    "t": {
                        "description": "d",
                        "task": "hi",
                        "tool": tool_str,
                        "depends_on": [],
                    }
                }
            ],
        }
    )
    assert c.tasks[0].tool == tool_str


def _task_with_tool(tool_str: str) -> dict:
    """Build a minimal conduit payload whose single task uses ``tool_str``.

    :param tool_str: the tool identifier to put on the task.
    """
    return {
        "name": "x",
        "description": "d",
        "tasks": [
            {"t": {"description": "d", "task": "hi", "tool": tool_str, "depends_on": []}}
        ],
    }


def test_unbundled_harness_name_validates():
    """An ACP agent the code has never heard of is a legal tool identifier.

    Harnesses are config (ATELIER_HARNESSES), so the schema checks the shape
    of the name only; whether one is registered is Atelier.tool_readiness's
    call.
    """
    c = Conduit.model_validate(_task_with_tool("harness:gemini"))
    assert c.tasks[0].tool == "harness:gemini"


@pytest.mark.parametrize(
    "tool_str",
    [
        "harness:codex:gpt-5.1-codex",
        "harness:claude-code:claude-sonnet-4-5",
        "harness:claude-code:opus[1m]",
        "harness:opencode:anthropic/claude-sonnet-4-5",
    ],
)
def test_harness_model_suffix_validates(tool_str):
    """A second colon names the model, spelled the way the agent lists it.

    :param tool_str: parametrized ``harness:<name>:<model>`` under test.
    """
    from flow_atelier.schemas.conduit import split_harness_tool

    c = Conduit.model_validate(_task_with_tool(tool_str))
    assert c.tasks[0].tool == tool_str
    base, model, effort = split_harness_tool(tool_str)
    assert base == tool_str.rsplit(":", 1)[0]
    assert model == tool_str.rsplit(":", 1)[1]
    assert effort is None


@pytest.mark.parametrize(
    ("tool_str", "expected"),
    [
        ("harness:codex:gpt-5.6-sol:high", ("harness:codex", "gpt-5.6-sol", "high")),
        ("harness:claude-code:opus[1m]:xhigh", ("harness:claude-code", "opus[1m]", "xhigh")),
        (
            "harness:opencode:anthropic/claude-sonnet-4-5:low",
            ("harness:opencode", "anthropic/claude-sonnet-4-5", "low"),
        ),
        ("harness:codex:gpt-5.5:ultra_2", ("harness:codex", "gpt-5.5", "ultra_2")),
    ],
)
def test_harness_effort_suffix_validates(tool_str, expected):
    """A third colon names the reasoning effort for that model.

    :param tool_str: parametrized ``harness:<name>:<model>:<effort>`` under test.
    :param expected: the three parts it must split into.
    """
    from flow_atelier.schemas.conduit import split_harness_tool

    c = Conduit.model_validate(_task_with_tool(tool_str))
    assert c.tasks[0].tool == tool_str
    assert split_harness_tool(tool_str) == expected


def test_split_harness_tool_without_model():
    """A plain harness tool and a built-in tool split to themselves with no suffixes."""
    from flow_atelier.schemas.conduit import split_harness_tool

    assert split_harness_tool("harness:codex") == ("harness:codex", None, None)
    assert split_harness_tool("tool:bash") == ("tool:bash", None, None)


@pytest.mark.parametrize(
    "tool_str",
    [
        "tool:rm-rf",       # tool:* executors are code — closed set
        "harness:",         # no name
        "harness:My Agent", # uppercase and a space
        "claude",           # missing the namespace prefix
        "harness:codex:",         # empty model
        "harness:codex:gpt 5",    # space in the model
        "harness:codex::high",    # effort with the model segment left empty
        "harness:codex:gpt-5.5:", # empty effort
        "harness:codex:gpt-5.5:hi gh",     # space in the effort
        "harness:codex:gpt-5.5:high:max",  # a fourth segment
        "harness:codex:gpt-5.5:-high",     # effort starting with a hyphen
    ],
)
def test_malformed_tool_is_rejected(tool_str):
    """Verify a tool identifier outside both grammars fails validation.

    :param tool_str: parametrized malformed identifier under test.
    """
    with pytest.raises(ValueError, match="invalid tool"):
        Conduit.model_validate(_task_with_tool(tool_str))


def test_repeat_must_be_positive():
    """Verify a task with repeat=0 is rejected by validation."""
    with pytest.raises(Exception):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {"a": {"description": "d", "task": "x", "tool": "tool:bash", "depends_on": [], "repeat": 0}}
                ],
            }
        )


@pytest.mark.parametrize("bad", [0, -1])
def test_task_timeout_must_be_positive(bad):
    """Verify an explicit per-task timeout below 1 is rejected.

    :param bad: a non-positive timeout value expected to fail validation.
    """
    with pytest.raises(Exception, match="timeout must be >= 1"):
        Conduit.model_validate(
            {
                "name": "x",
                "description": "d",
                "tasks": [
                    {"a": {"description": "d", "task": "x", "tool": "tool:bash", "depends_on": [], "timeout": bad}}
                ],
            }
        )


def test_task_timeout_none_and_positive_ok():
    """Verify an omitted timeout defaults to None and a positive value loads."""
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "tasks": [
                {"a": {"description": "d", "task": "x", "tool": "tool:bash", "depends_on": []}},
                {"b": {"description": "d", "task": "x", "tool": "tool:bash", "depends_on": [], "timeout": 60}},
            ],
        }
    )
    assert c.tasks[0].timeout is None
    assert c.tasks[1].timeout == 60


def _task_with_until(**overrides):
    """Build a Conduit payload with an `until` task, applying overrides.

    :param overrides: keyword overrides merged into the base task body.
    """
    base = {
        "description": "d",
        "task": "x",
        "tool": "tool:bash",
        "depends_on": [],
        "repeat": 3,
        "until": "output.match(DONE)",
    }
    base.update(overrides)
    return {
        "name": "x",
        "description": "d",
        "tasks": [{"a": base}],
    }


def test_until_with_repeat_gt_1_ok():
    """Verify `until` is accepted when repeat is greater than 1."""
    c = Conduit.model_validate(_task_with_until())
    assert c.tasks[0].until == "output.match(DONE)"


def test_until_not_match_with_repeat_gt_1_ok():
    """Verify `until` with not_match is accepted when repeat > 1."""
    c = Conduit.model_validate(_task_with_until(until="output.not_match(RETRY)"))
    assert c.tasks[0].until == "output.not_match(RETRY)"


def test_until_with_repeat_1_rejected():
    """Verify `until` is rejected when repeat is 1."""
    with pytest.raises(Exception, match="repeat"):
        Conduit.model_validate(_task_with_until(repeat=1))


def test_until_with_invalid_dsl_rejected():
    """Verify invalid DSL in `until` is rejected."""
    with pytest.raises(Exception):
        Conduit.model_validate(_task_with_until(until="DONE"))


def test_until_with_invalid_regex_rejected():
    """Verify invalid regex in `until` is rejected."""
    with pytest.raises(Exception):
        Conduit.model_validate(_task_with_until(until="output.match([unclosed)"))


def test_task_without_until_still_validates():
    """Verify a task with no `until` and repeat > 1 still validates."""
    c = Conduit.model_validate(
        {
            "name": "x",
            "description": "d",
            "tasks": [
                {"a": {"description": "d", "task": "x", "tool": "tool:bash",
                       "depends_on": [], "repeat": 2}}
            ],
        }
    )
    assert c.tasks[0].until is None


# ------------------------------------------------------------------ while


def _task_with_while(**overrides):
    """Build a Conduit payload with a `while` task, applying overrides.

    :param overrides: keyword overrides merged into the base task body.
    """
    base = {
        "description": "d",
        "task": "x",
        "tool": "tool:bash",
        "depends_on": [],
        "repeat": 3,
        "while": "output.match(retry)",
    }
    base.update(overrides)
    return {
        "name": "x",
        "description": "d",
        "tasks": [{"a": base}],
    }


def test_while_yaml_key_loads_into_while_attr():
    """Verify the YAML `while` key populates the `while_` attribute."""
    c = Conduit.model_validate(_task_with_while())
    assert c.tasks[0].while_ == "output.match(retry)"


def test_while_not_match_with_repeat_gt_1_ok():
    """Verify `while` with not_match is accepted when repeat > 1."""
    c = Conduit.model_validate(_task_with_while(**{"while": "output.not_match(ready)"}))
    assert c.tasks[0].while_ == "output.not_match(ready)"


def test_while_with_repeat_1_rejected():
    """Verify `while` is rejected when repeat is 1."""
    with pytest.raises(Exception, match="repeat"):
        Conduit.model_validate(_task_with_while(repeat=1))


def test_while_and_until_mutually_exclusive():
    """Verify `while` and `until` cannot be combined on a single task."""
    body = _task_with_while(until="output.match(DONE)")
    with pytest.raises(Exception) as exc:
        Conduit.model_validate(body)
    msg = str(exc.value)
    assert "until" in msg and "while" in msg


def test_while_with_invalid_dsl_rejected():
    """Verify invalid DSL in `while` is rejected."""
    with pytest.raises(Exception):
        Conduit.model_validate(_task_with_while(**{"while": "retry"}))


def test_while_with_invalid_regex_rejected():
    """Verify invalid regex in `while` is rejected."""
    with pytest.raises(Exception):
        Conduit.model_validate(_task_with_while(**{"while": "output.match([unclosed)"}))


def test_while_round_trips_to_yaml_alias():
    """Verify `while_` round-trips back to the `while` YAML alias."""
    c = Conduit.model_validate(_task_with_while())
    dumped = c.tasks[0].model_dump(by_alias=True)
    assert "while" in dumped
    assert dumped["while"] == "output.match(retry)"
    assert "while_" not in dumped


def test_flow_id_roundtrip():
    """Verify a generated flow id parses back into its components."""
    fid = new_flow_id("deploy_pipeline")
    assert FLOW_ID_RE.match(fid)
    conduit, uuid8, date = parse_flow_id(fid)
    assert conduit == "deploy_pipeline"
    assert len(uuid8) == 8
    assert re.match(r"^\d{8}$", date)


def test_parse_flow_id_rejects_invalid():
    """Verify parse_flow_id raises ValueError for malformed input."""
    with pytest.raises(ValueError):
        parse_flow_id("not-a-flow-id")


def test_progress_roundtrip():
    """Verify Progress serializes to JSON and round-trips faithfully."""
    p = Progress(
        status=FlowStatus.running,
        tasks={"a": TaskProgress(status=TaskStatus.completed, iteration=1, of=1)},
        started_at="2026-04-12T10:00:00Z",
    )
    as_json = p.model_dump_json()
    restored = Progress.model_validate_json(as_json)
    assert restored.tasks["a"].status == TaskStatus.completed


# --- unknown configuration fields -------------------------------------------
# Pydantic's default is to discard an extra key, which turns a misspelled
# control into a silent change of meaning: `depend_on:` becomes a task with no
# dependencies that validates, plans and runs as if the author wanted that.


def _two_tasks(second: dict) -> dict:
    """Build a conduit whose second task body is ``second``.

    :param second: the task body to place after ``prepare``.
    :returns: a raw conduit payload ready for ``model_validate``.
    """
    return {
        "name": "typo_demo",
        "description": "d",
        "tasks": [
            {
                "name": "prepare",
                "description": "first",
                "task": "echo one",
                "tool": "tool:bash",
            },
            second,
        ],
    }


GOOD_SECOND = {
    "name": "consume",
    "description": "second",
    "task": "echo two",
    "tool": "tool:bash",
    "depends_on": ["prepare"],
}


@pytest.mark.parametrize(
    ("document", "loc"),
    [
        # A typo on the conduit root.
        (
            {**_two_tasks(GOOD_SECOND), "max_concurreny": 5},
            ("max_concurreny",),
        ),
        # A typo in a normalized task body...
        (
            _two_tasks({**GOOD_SECOND, "depends_on": [], "depend_on": ["prepare"]}),
            ("tasks", 1, "depend_on"),
        ),
        # ...and the same typo in the wrapped form the YAML also accepts.
        (
            _two_tasks({"consume": {
                "description": "second",
                "task": "echo two",
                "tool": "tool:bash",
                "depend_on": ["prepare"],
            }}),
            ("tasks", 1, "depend_on"),
        ),
        # A typo beside the correctly spelled field: the real `timeout` cannot
        # cover for the misspelling.
        (
            _two_tasks({**GOOD_SECOND, "timeout": 30, "timout": 60}),
            ("tasks", 1, "timout"),
        ),
        # A typo inside an object-form input specification.
        (
            {
                **_two_tasks(GOOD_SECOND),
                "inputs": {"topic": {"description": "t", "defaut": "x"}},
            },
            ("inputs", "topic", "defaut"),
        ),
    ],
)
def test_unknown_configuration_fields_are_rejected_where_they_are(document, loc):
    """A misspelled control fails to load, and the error names it."""
    with pytest.raises(ValidationError) as ei:
        Conduit.model_validate(document)

    locations = [e["loc"] for e in ei.value.errors()]
    assert loc in locations, locations


def test_the_repaired_spelling_is_accepted_and_actually_connects_the_tasks():
    """Fixing only the spelling turns the dropped key into a real edge."""
    conduit = Conduit.model_validate(_two_tasks(GOOD_SECOND))
    assert conduit.tasks[1].depends_on == ["prepare"]


def test_author_supplied_map_keys_stay_open():
    """Input names and a task's forwarded bindings are data, not fields."""
    conduit = Conduit.model_validate({
        "name": "open_maps",
        "description": "d",
        # Keys that look exactly like configuration names, because a caller is
        # free to name an input anything.
        "inputs": {"timeout": "how long", "depends_on": "a red herring"},
        "tasks": [{
            "name": "call",
            "description": "d",
            "task": "child",
            "tool": "tool:conduit",
            "inputs": {"retries": "7", "nested": {"any": ["shape"]}},
        }],
    })
    assert set(conduit.inputs) == {"timeout", "depends_on"}
    assert conduit.tasks[0].inputs == {"retries": "7", "nested": {"any": ["shape"]}}


def test_a_conduit_still_round_trips_through_its_own_dump():
    """`model_dump` output must reload — including the internal `while_` key."""
    original = Conduit.model_validate(_task_with_while())
    assert Conduit.model_validate(original.model_dump()) == original
    assert Conduit.model_validate(original.model_dump(by_alias=True)) == original
