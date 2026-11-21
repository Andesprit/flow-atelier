"""Machine-readable evidence and decision rules for fixed-workload experiments."""
import ast
import json
import math
import tokenize
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from statistics import median

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt, PositiveFloat


class DriverReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False,
                              hide_input_in_errors=True)


class MetricsSettings(DriverReport):
    unit: str = Field(min_length=1)
    min_gain_percent: PositiveFloat = 5
    input_per_million: NonNegativeFloat | None = None
    output_per_million: NonNegativeFloat | None = None


class TokenUsage(DriverReport):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


class CodeMetrics(DriverReport):
    complexity: NonNegativeFloat
    function_length: NonNegativeInt
    duplication: NonNegativeFloat
    dead_code: NonNegativeInt
    lint_warnings: NonNegativeInt
    type_warnings: NonNegativeInt


class BrowserTask(DriverReport):
    steps: int = Field(ge=1)
    task_completed: bool
    keyboard_passed: bool
    accessibility_violations: NonNegativeInt


def python_metrics(root: Path, invoke: Callable) -> dict:
    """Collect Python guardrails with the target's installed analysis tools."""
    names = invoke("git", "ls-files", "-z", "--", "*.py").stdout.rstrip("\0").split("\0")
    if not names[0]:
        raise ValueError("provide docs/improvement/clean-code.sh for this language")
    longest = 0
    windows = Counter()
    for name in names:
        path = root / name
        if path.resolve() != path:
            raise ValueError(f"linked Python source: {name}")
        with tokenize.open(path) as file:
            text = file.read()
        for node in ast.walk(ast.parse(text, filename=name)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                longest = max(longest, node.end_lineno - node.lineno + 1)
        lines = [line.strip() for line in text.splitlines() if line.strip()
                 and not line.lstrip().startswith("#")]
        windows.update(tuple(lines[i:i + 6]) for i in range(len(lines) - 5))

    def report(*cmd: str, codes: tuple = (0,)) -> str:
        result = invoke(*cmd, check=False)
        if result.returncode not in codes:
            raise ValueError(f"{cmd[0]} analysis failed (exit {result.returncode})")
        return result.stdout

    radon = json.loads(report("radon", "cc", "--json", "."))
    blocks = []
    for group in radon.values():
        if not isinstance(group, list):
            raise ValueError("Radon could not analyze all files")
        blocks.extend(group)
    complexity = 0
    while blocks:
        block = blocks.pop()
        complexity = max(complexity, block["complexity"])
        blocks.extend(block.get("methods", []))
        blocks.extend(block.get("closures", []))
    lint = json.loads(report("ruff", "check", "--no-fix", "--output-format", "json", ".",
                             codes=(0, 1)))
    dead = report("vulture", ".", "--min-confidence", "100", codes=(0, 3))
    types = json.loads(report("pyright", "--outputjson", codes=(0, 1)))["summary"]
    return CodeMetrics(
        complexity=complexity, function_length=longest,
        duplication=sum(count - 1 for count in windows.values()), dead_code=len(dead.splitlines()),
        lint_warnings=len(lint), type_warnings=types["errorCount"] + types["warningCount"],
    ).model_dump()


def numeric_sample(goal: str, output: str, settings: MetricsSettings) -> float:
    if goal == "cost" and output.lstrip().startswith("{"):
        usage = TokenUsage.model_validate_json(output)
        if settings.input_per_million is None or settings.output_per_million is None:
            raise ValueError("token cost measurement requires input and output prices per million")
        amount = (usage.input_tokens * settings.input_per_million
                  + usage.output_tokens * settings.output_per_million) / 1_000_000
    else:
        try:
            amount = float(output)
        except ValueError:
            raise ValueError(f"{goal} driver did not report a number") from None
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{goal} driver must report a finite non-negative measurement")
    return amount


def browser_sample(task_output: str, lighthouse: str) -> dict:
    sample = BrowserTask.model_validate_json(task_output).model_dump()
    try:
        categories = json.loads(lighthouse)["categories"]
        for name in ("performance", "accessibility"):
            score = categories[name]["score"]
            if type(score) not in (float, int) or not 0 <= score <= 1:
                raise ValueError
            sample["lighthouse_" + name] = score
    except (KeyError, ValueError, TypeError):
        raise ValueError("incomplete Lighthouse performance/accessibility report") from None
    return sample


def compare_samples(
    before: list[float], after: list[float], floor: float, *, higher_better: bool = False,
) -> tuple[bool, str]:
    """Require the median paired gain to exceed the observed baseline range."""
    if any(value <= 0 for value in before):
        return False, "No positive baseline for percentage comparison"
    gain = median((a - b) / a * 100 for a, b in zip(before, after, strict=True))
    if higher_better:
        gain = -gain
    noise = 100 * (max(before) - min(before)) / median(before)
    return gain >= floor and gain > noise, (
        f"median paired gain {gain:.2f}%, baseline noise {noise:.2f}%, minimum {floor:g}%"
    )


def assess_ux(before: list[dict], after: list[dict], floor: float) -> tuple[bool, str]:
    for check in ("keyboard_passed", "task_completed"):
        if not all(row[check] for row in before + after):
            return False, f"Fixed browser task failed {check}"
    if max(row["accessibility_violations"] for row in after) > max(
        row["accessibility_violations"] for row in before
    ):
        return False, "Browser accessibility violations increased"
    gains = []
    for metric in ("steps", "lighthouse_accessibility", "lighthouse_performance"):
        baseline = [row[metric] for row in before]
        candidate = [row[metric] for row in after]
        higher = metric != "steps"
        if ((median(candidate) < median(baseline)) if higher
                else (median(candidate) > median(baseline))):
            return False, f"{metric} regressed"
        passed, reason = compare_samples(baseline, candidate, floor, higher_better=higher)
        if passed:
            gains.append(f"{metric}: {reason}")
    return (True, ", ".join(gains)) if gains else (False, "No UX gain above threshold and noise")
