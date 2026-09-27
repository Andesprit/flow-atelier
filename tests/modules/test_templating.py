"""Templating unit tests."""
from pathlib import Path

import pytest

from flow_atelier.modules.templating import (
    SkipSignal,
    TemplateError,
    TemplateRef,
    extract_task_refs,
    extract_template_refs,
    resolve,
)


def test_resolve_inputs():
    """Verify resolve() substitutes a single input placeholder."""
    out = resolve("hello {{inputs.name}}!", {"name": "world"}, {})
    assert out == "hello world!"


def test_resolve_multiple():
    """Verify resolve() substitutes multiple input placeholders."""
    out = resolve(
        "{{inputs.a}}-{{inputs.b}}-{{inputs.a}}",
        {"a": "x", "b": "y"},
        {},
    )
    assert out == "x-y-x"


def test_resolve_missing_input_raises():
    """Verify resolve() raises TemplateError on missing input."""
    with pytest.raises(TemplateError):
        resolve("{{inputs.missing}}", {}, {})


def test_resolve_task_output():
    """Verify resolve() substitutes a task output reference."""
    out = resolve("v={{get_version.output}}", {}, {"get_version": "1.2.3"})
    assert out == "v=1.2.3"


def test_resolve_mixed():
    """Verify resolve() handles mixed input and task-output references."""
    out = resolve(
        "echo '{{inputs.env}}: {{build.output}}'",
        {"env": "prod"},
        {"build": "ok"},
    )
    assert out == "echo 'prod: ok'"


def test_resolve_task_unavailable_raises_skip():
    """Verify resolve() raises SkipSignal for unavailable tasks."""
    with pytest.raises(SkipSignal):
        resolve("{{a.output}}", {}, {}, unavailable_tasks={"a"})


def test_resolve_task_not_yet_completed_raises_skip():
    """Verify resolve() raises SkipSignal when task output is absent."""
    with pytest.raises(SkipSignal):
        resolve("{{a.output}}", {}, {})


def test_resolve_unknown_expression():
    """Verify resolve() raises TemplateError on unknown expression."""
    with pytest.raises(TemplateError):
        resolve("{{weird}}", {}, {})


def test_resolve_non_template_string_unchanged():
    """Verify resolve() returns plain strings unchanged."""
    assert resolve("plain text", {}, {}) == "plain text"


def test_resolve_loop_previous_empty_on_first_iteration():
    """Verify {{loop.previous}} is empty when no iterations have run."""
    assert resolve("p=[{{loop.previous}}]", {}, {}) == "p=[]"


def test_resolve_loop_previous_returns_last_output():
    """Verify {{loop.previous}} resolves to the most recent iteration output."""
    out = resolve("p={{loop.previous}}", {}, {}, loop_history=["a", "b"])
    assert out == "p=b"


def test_resolve_loop_history_empty_on_first_iteration():
    """Verify {{loop.history}} is empty when no iterations have run."""
    assert resolve("h=[{{loop.history}}]", {}, {}) == "h=[]"


def test_resolve_loop_history_numbers_iterations():
    """Verify {{loop.history}} renders numbered, separated iteration blocks."""
    out = resolve("{{loop.history}}", {}, {}, loop_history=["a", "b"])
    assert out == "--- iteration 1 ---\na\n\n--- iteration 2 ---\nb"


def test_resolve_loop_history_capped_with_omitted_header():
    """Verify {{loop.history}} keeps the newest entries and notes omissions."""
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=["a", "b", "c"], loop_history_limit=2,
    )
    assert out == (
        "--- 1 earlier iterations omitted ---\n\n"
        "--- iteration 2 ---\nb\n\n"
        "--- iteration 3 ---\nc"
    )


def test_resolve_loop_history_limit_zero_means_unlimited():
    """Verify loop_history_limit=0 renders the full history."""
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=["a", "b"], loop_history_limit=0,
    )
    assert out == "--- iteration 1 ---\na\n\n--- iteration 2 ---\nb"


def test_resolve_loop_history_under_limit_unchanged():
    """Verify no omission header appears when history fits the limit."""
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=["a"], loop_history_limit=10,
    )
    assert out == "--- iteration 1 ---\na"


def test_resolve_loop_history_entry_truncated_head_and_tail():
    """Verify oversized history entries keep head and tail around a marker."""
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=["A" * 6 + "MIDDLE" + "B" * 6],
        loop_history_entry_chars=12,
    )
    assert out == (
        "--- iteration 1 ---\n"
        "AAAAAA\n[... 6 chars truncated ...]\nBBBBBB"
    )


def test_resolve_loop_history_entry_under_budget_unchanged():
    """Verify entries within the char budget render untouched."""
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=["short"], loop_history_entry_chars=100,
    )
    assert out == "--- iteration 1 ---\nshort"


def test_resolve_loop_history_entry_chars_zero_unlimited():
    """Verify entry_chars <= 0 disables per-entry truncation."""
    big = "x" * 500
    out = resolve(
        "{{loop.history}}", {}, {},
        loop_history=[big], loop_history_entry_chars=0,
    )
    assert big in out


def test_resolve_conduit_dir_substitutes_absolute_path():
    """Verify {{conduit_dir}} resolves to the supplied dir's absolute string."""
    base = Path("/a/b")
    out = resolve("{{conduit_dir}}/x", {}, {}, conduit_dir=base)
    assert out == f"{base}/x"


def test_resolve_conduit_dir_none_falls_through_to_unknown():
    """Verify {{conduit_dir}} raises TemplateError when no conduit_dir given."""
    with pytest.raises(TemplateError):
        resolve("{{conduit_dir}}", {}, {})


def test_extract_template_refs_classifies_conduit_dir():
    """Verify {{conduit_dir}} is classified as a known 'conduit_dir' kind."""
    refs = extract_template_refs("{{conduit_dir}}")
    assert refs == [TemplateRef("conduit_dir", "conduit_dir", "conduit_dir")]


def test_extract_task_refs_finds_output_refs():
    """Verify extract_task_refs returns task names from .output expressions."""
    refs = extract_task_refs("a={{build.output}} b={{ test.output }}")
    assert refs == {"build", "test"}


def test_extract_task_refs_ignores_inputs_and_loop():
    """Verify extract_task_refs skips inputs.* and loop.* expressions."""
    refs = extract_task_refs(
        "{{inputs.x}} {{loop.previous}} {{loop.history}} {{real.output}}"
    )
    assert refs == {"real"}


def test_extract_task_refs_empty_for_plain_text():
    """Verify extract_task_refs returns an empty set without templates."""
    assert extract_task_refs("echo plain") == set()


def test_extract_template_refs_classifies_each_form():
    """Verify each grammar form is classified with the right kind/value."""
    refs = extract_template_refs(
        "{{inputs.x}} {{loop.previous}} {{loop.history}} {{build.output}}"
    )
    assert refs == [
        TemplateRef("input", "x", "inputs.x"),
        TemplateRef("loop", "loop.previous", "loop.previous"),
        TemplateRef("loop", "loop.history", "loop.history"),
        TemplateRef("task", "build", "build.output"),
    ]


def test_extract_template_refs_flags_unrecognized():
    """Verify malformed expressions are classified as 'unknown'."""
    for expr in ("inputs", "job.outpt", "loop.histroy"):
        refs = extract_template_refs(f"{{{{{expr}}}}}")
        assert refs == [TemplateRef("unknown", expr, expr)]


def test_extract_template_refs_empty_for_plain_text():
    """Verify plain text yields no refs."""
    assert extract_template_refs("echo plain") == []


def test_extract_template_refs_preserves_source_order():
    """Verify refs are returned in source-appearance order."""
    refs = extract_template_refs("{{b.output}} {{inputs.a}}")
    assert [r.value for r in refs] == ["b", "a"]


# ------------------------------------------------------- {{<task>.tool}}


def test_task_tool_resolves_to_the_tool_that_task_runs_on():
    """A prompt can name the agent behind a result it quotes."""
    out = resolve(
        "from {{build.tool}}: {{build.output}}",
        {},
        {"build": "done"},
        task_tools={"build": "harness:codex"},
    )
    assert out == "from harness:codex: done"


def test_task_tool_reports_the_effective_tool_not_the_recipe_default():
    """The caller passes what ran, so a re-pointed task attributes correctly."""
    out = resolve(
        "{{build.tool}}", {}, {}, task_tools={"build": "harness:gemini:pro"}
    )
    assert out == "harness:gemini:pro"


def test_task_tool_does_not_skip_for_a_task_with_no_output_yet():
    """A tool is known whether or not the task has produced anything."""
    assert resolve("{{build.tool}}", {}, {}, task_tools={"build": "harness:x"}) == (
        "harness:x"
    )


def test_task_tool_of_an_unknown_task_is_a_hard_error():
    """Nothing can be attributed to a task the conduit does not define."""
    with pytest.raises(TemplateError, match="unknown task"):
        resolve("{{ghost.tool}}", {}, {}, task_tools={"build": "harness:x"})


def test_extract_template_refs_classifies_task_tool():
    """Validation sees `.tool` as a task reference of its own kind."""
    assert extract_template_refs("{{build.tool}}") == [
        TemplateRef("task_tool", "build", "build.tool")
    ]
