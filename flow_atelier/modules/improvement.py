"""Measure a single hypothesis against origin/main and publish its decision."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from statistics import median

from pydantic import BaseModel, Field

from flow_atelier.core.atelier import Atelier
from flow_atelier.schemas.conduit import Conduit, TaskDefinition
from flow_atelier.services.store.claims import EventClaims

LOG = Path("docs/improvement-log.md")
DRIVERS = Path("docs/improvement")
PREFIX = "atelier/improvements/"


class Hypothesis(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,59}$")
    hypothesis: str = Field(min_length=1, max_length=1000)


def _cmd(cwd: Path, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if cmd[0] == "gh":
        env.pop("GH_REPO", None)  # Resolve the isolated clone's single origin.
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{Path(cmd[0]).name} timed out") from None
    if check and result.returncode:
        # git/gh errors can echo authenticated remotes or provider response bodies.
        raise ValueError(f"{Path(cmd[0]).name} {cmd[1]} exited {result.returncode}")
    return result


def _git(cwd: Path, *args: str) -> str:
    return _cmd(cwd, "git", *args).stdout.strip()


def _local(root: Path, name: Path) -> Path:
    path = root / name
    if path.resolve() != path:
        raise ValueError(f"expected an unlinked repository path: {name}")
    return path


@contextmanager
def _baseline(project: Path):
    project = project.resolve(strict=True)
    origin = _git(project, "remote", "get-url", "origin")
    with tempfile.TemporaryDirectory(prefix="atelier-improvement-") as folder:
        base = Path(folder).resolve() / "baseline"
        _git(project, "clone", "--quiet", "--no-local", "--single-branch", "--branch", "main",
             "--", origin, str(base))
        yield base, origin


def _inventory(project: Path, base: Path) -> tuple[list, set[str]]:
    prs = json.loads(_cmd(
        base, "gh", "pr", "list", "--state", "open", "--limit", "1000",
        "--json", "headRefName,title,url",
    ).stdout)
    if len(prs) >= 1000:
        raise ValueError("open PR inventory exceeds 1000; could not check all open improvements")
    prs = [pr for pr in prs if pr["headRefName"].startswith(PREFIX)]
    remote = _git(project, "ls-remote", "--heads", "origin", f"refs/heads/{PREFIX}*")
    branches = {line.split("\t")[1].removeprefix("refs/heads/") for line in remote.splitlines()}
    branches.update(_git(project, "for-each-ref", "--format=%(refname:short)",
                         f"refs/heads/{PREFIX}").splitlines())
    return prs, branches


def _decisions(base: Path) -> str:
    path = _local(base, LOG)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _settings(base: Path, goal: str) -> tuple[str, float]:
    if goal != "latency":
        raise ValueError(f"unsupported improvement goal: {goal}")
    path = _local(base, DRIVERS / "settings.json")
    config = json.loads(path.read_text(encoding="utf-8")).get(goal, {}) if path.exists() else {}
    floor = float(config.get("min_gain_percent", 5))
    unit = config.get("unit", "ms")
    if not math.isfinite(floor) or floor <= 0 or not isinstance(unit, str) or not unit.strip():
        raise ValueError("expected a positive min_gain_percent and named unit")
    return unit, floor


def _driver(base: Path, checkout: Path, name: str, *, check: bool = True):
    script = _local(base, DRIVERS / f"{name}.sh")
    if not script.is_file():
        raise ValueError(f"missing fixed driver {DRIVERS}/{name}.sh")
    return _cmd(checkout, "bash", "-e", "-o", "pipefail", str(script), check=check)


def _sample(base: Path, checkout: Path, goal: str) -> float:
    output = _driver(base, checkout, goal).stdout.strip()
    try:
        result = float(output)
    except ValueError:
        raise ValueError(f"{goal} driver did not report a number") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{goal} driver must report a finite non-negative measurement")
    return result


def compare_samples(before: list[float], after: list[float], floor: float) -> tuple[bool, str]:
    """Require the median paired gain to exceed the observed baseline range."""
    if any(value <= 0 for value in before):
        return False, "No positive baseline for percentage comparison"
    gain = median((a - b) / a * 100 for a, b in zip(before, after, strict=True))
    noise = 100 * (max(before) - min(before)) / median(before)
    return gain >= floor and gain > noise, (
        f"median paired gain {gain:.2f}%, baseline noise {noise:.2f}%, minimum {floor:g}%"
    )


async def _agent(
    atelier: Atelier, parent: str, cwd: Path, name: str, tool: str, prompt: str,
) -> str:
    task = TaskDefinition(name=name, tool=tool, task="{{inputs.prompt}}", description=name,
                          permissions="edits" if name == "build" else "read-only")
    conduit = Conduit(name="improvement-agent", description=name, tasks=[task])
    child = await atelier.engine.run(conduit, {"prompt": prompt}, working_dir=cwd,
                                     parent_flow_id=parent, invoking_task=name, show_steps=False)
    return atelier.get_outputs(child)[name].strip()


async def propose(atelier: Atelier, run: str, project: Path, goal: str) -> dict:
    """Inspect the main branch, active work and baseline before asking for one hypothesis."""
    with _baseline(project) as (base, _origin):
        unit, _ = _settings(base, goal)
        decisions = _decisions(base)
        prs, branches = _inventory(project, base)
        if _driver(base, base, "test", check=False).returncode:
            raise ValueError("behavior checks already fail on origin/main")
        sample = _sample(base, base, goal)
        context = json.dumps({"goal": goal, "baseline": sample, "unit": unit,
                              "decision_log": decisions, "open_prs": prs,
                              "improvement_branches": sorted(branches)})
        answer = await _agent(
            atelier, run, base, "propose", "harness:codex",
            "Inspect this checkout. Propose one change with a measurable hypothesis. "
            "Exclude ideas already recorded or in progress. Reuse the same canonical key "
            "for the same hypothesis. Return only JSON with key (short lowercase slug) "
            f"and hypothesis (one sentence). Evidence:\n{context}",
        )
        return {**Hypothesis.model_validate_json(answer).model_dump(),
                "base": _git(base, "rev-parse", "HEAD"), "initial": sample}


def _record(checkout: Path, data: dict, marker: str) -> None:
    log = _local(checkout, LOG)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as file:
        file.write(f"\n{marker}\n### {data['goal']} / {data['key']}\n\n")
        file.write(f"{data['reason']}\n\n```json\n")
        file.write(json.dumps(data, indent=2, ensure_ascii=True) + "\n```\n")
    _git(checkout, "add", "--", LOG.as_posix())


def _open_pr(base: Path, branch: str, key: str) -> str:
    body = base.parent / "pr.md"
    body.write_text(f"Review the {key} experiment and its measurements in `{LOG}`.\n")
    return _cmd(base, "gh", "pr", "create", "--base", "main",
                "--head", branch, "--title", f"Improvement experiment: {key}",
                "--body-file", str(body)).stdout.strip()


async def experiment(
    atelier: Atelier, run: str, project: Path, goal: str, schedule: str, proposal: dict,
) -> dict:
    hypothesis = Hypothesis.model_validate(proposal)
    journal = atelier.store.journal(run)
    if not journal or journal.header()["conduit"] != "continuous-improvement":
        raise ValueError("expected a continuous-improvement run journal")
    with _baseline(project) as (base, origin):
        unit, floor = _settings(base, goal)
        branch = f"{PREFIX}{goal}/{hypothesis.key}"
        marker = f"<!-- atelier-improvement {goal}/{hypothesis.key} -->"
        if marker in _decisions(base).splitlines():
            return {"reason": "hypothesis already in decision log"}
        prs, branches = _inventory(project, base)
        for pr in prs:
            if pr["headRefName"] == branch:
                return {"reason": "improvement PR already open", "pr": pr["url"]}
        if proposal["base"] != _git(base, "rev-parse", "HEAD"):
            raise ValueError("origin/main changed since proposal; start a new experiment")
        claim_key = hashlib.sha256(json.dumps([origin, branch]).encode()).hexdigest()
        owner = EventClaims(atelier.settings.global_atelier_dir, "improvements").reserve_run(
            claim_key, Path(journal.header()["project"]), run,
        )
        if owner.run_id != run:
            return {"reason": "hypothesis already reserved", "run_id": owner.run_id}
        if branch in branches:
            # A previous attempt may have pushed successfully before gh returned its URL.
            ref = f"refs/heads/{branch}"
            if _git(project, "ls-remote", "--heads", "origin", ref):
                _git(base, "fetch", "--quiet", "origin", ref)
                saved = _git(base, "show", f"FETCH_HEAD:{LOG.as_posix()}")
                if marker in saved.splitlines() and f'"run_id": "{run}"' in saved:
                    return {"reason": "recovered published branch",
                            "pr": _open_pr(base, branch, hypothesis.key)}
            return {"reason": "improvement branch already exists"}

        candidate = base.parent / "candidate"
        _git(base, "clone", "--quiet", "--no-local", "--", str(base), str(candidate))
        _git(candidate, "switch", "-c", branch)
        await _agent(atelier, run, candidate, "build", "harness:claude-code",
                     f"Implement only this {goal} hypothesis: {hypothesis.hypothesis}. "
                     "Edit source files. Keep behavior, tests and measurement drivers intact. "
                     "The coordinator handles testing, Git and publication.")
        if _git(candidate, "rev-parse", "HEAD") != proposal["base"]:
            raise ValueError("builder changed the experiment's base commit")
        _git(candidate, "add", "-A")
        paths = _cmd(candidate, "git", "diff", "--cached", "--no-renames",
                     "--name-only", "-z").stdout.split("\0")
        paths = [Path(p) for p in paths if p]
        protected = any(p == LOG or p.is_relative_to(DRIVERS) or p.parts[0] in {"test", "tests"}
                        for p in paths)
        baseline_samples, candidate_samples = [], []
        accepted = False
        reason = "No source changes" if not paths else "Change modified tests or measurement files"
        if paths and not protected:
            _git(candidate, "commit", "-qm", f"Test hypothesis {hypothesis.key}")
            tests = _driver(base, candidate, "test", check=False)
            try:
                for index in range(5):
                    pair = [(base, baseline_samples), (candidate, candidate_samples)]
                    for tree, samples in (reversed(pair) if index % 2 else pair):
                        samples.append(_sample(base, tree, goal))
            except ValueError as exc:
                reason = f"Measurement failed: {exc}"
            else:
                accepted, reason = compare_samples(baseline_samples, candidate_samples, floor)
            if tests.returncode:
                accepted, reason = False, f"Behavior checks failed (exit {tests.returncode})"
        data = {**hypothesis.model_dump(), "run_id": run, "goal": goal, "schedule": schedule,
                "base": proposal["base"], "unit": unit, "baseline": baseline_samples,
                "candidate": candidate_samples, "accepted": accepted, "reason": reason}
        publish = base.parent / "publish"
        revision = proposal["base"]
        if accepted:
            _git(base, "fetch", "--quiet", str(candidate), branch)
            revision = "FETCH_HEAD"
        _git(base, "worktree", "add", "--detach", str(publish), revision)
        _record(publish, data, marker)
        _git(publish, "commit", "-qm", f"Record {goal} decision for {hypothesis.key}")
        ref = f"refs/heads/{branch}"
        _git(publish, "push", f"--force-with-lease={ref}:", "origin", f"HEAD:{ref}")
        return {**data, "branch": branch,
                "pr": _open_pr(base, branch, hypothesis.key)}

