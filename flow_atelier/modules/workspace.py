"""Per-task Git worktrees — a separate checkout for each parallel worker.

``--worktree TASK`` gives one top-level task its own detached Git worktree,
cut from the source checkout's HEAD before any task of the run starts. Two
coding agents can then edit the same relative file without overwriting each
other, and both results survive on disk for a human to read.

Nothing is merged, reset, stashed, pushed or deleted. A worktree outlives its
run: it is stored beside the flow records rather than inside one, so removing
a flow never takes an agent's edits with it. Cleaning up is the user's call.

A worktree is filesystem separation, not a sandbox. It shares the repository's
object store and puts no limit on what a process can open, bind or spend.

Only the destination side is this module's business: whether a *task* may be
selected is a question about the recipe (:func:`check_selectors`), and where
the worktrees go is the store's (:meth:`FilesystemStore.workspace_dir`).
"""
from __future__ import annotations

import difflib
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.progress import Workspaces

# The tools a separate checkout means anything for: something that edits files.
# A nested conduit runs its own flow (with its own selection), and a human
# approval step has no working directory to give.
ISOLATABLE_TOOL_PREFIXES = ("harness:", "tool:bash")


class WorkspaceError(ValueError):
    """A worktree request the user has to fix, carrying its own exit code.

    :param message: the actionable diagnostic.
    :param code: process exit status — 2 for a misuse of the option itself,
        1 for a refusal about the world (no such task, dirty source, a
        recorded worktree that is gone).
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceRepo:
    """The checkout every worktree of one run is cut from.

    :param root: absolute path of the source checkout's top level.
    :param head: the exact commit the worktrees are created at.
    """

    root: Path
    head: str


def parse_worktree_selectors(
    raw: Iterable[str] | None, label: str = "--worktree"
) -> list[str]:
    """Parse repeated ``--worktree TASK`` values into task names.

    A plain name, because the product chooses the directory: a user-supplied
    path would make two runs able to collide on one checkout, which is the
    thing this feature exists to prevent.

    :param raw: the option values as typed, in order.
    :param label: the option name, for the diagnostics.
    :returns: the selected task names, in order, without duplicates.
    :raises WorkspaceError: a value is empty or names a task twice.
    """
    out: list[str] = []
    for item in raw or []:
        name = item.strip()
        if not name:
            raise WorkspaceError(
                f"{label} {item!r}: expected a task name, for example "
                f"{label} step_1"
            )
        if "=" in name:
            raise WorkspaceError(
                f"{label} {item!r}: expected a task name only — the directory "
                "is chosen per run so two runs cannot share one checkout"
            )
        if name in out:
            raise WorkspaceError(
                f"{label} names task {name!r} twice; select it once"
            )
        out.append(name)
    return out


def check_selectors(
    conduit: Conduit, tasks: Iterable[str], *, origin: str = "--worktree"
) -> None:
    """Refuse a selection the recipe cannot honour, before anything is created.

    Top-level only, and only for a task that actually works on files. A
    same-named task inside a nested ``tool:conduit`` is untouched: that child
    is a flow of its own and makes its own selection.

    :param conduit: the recipe as it will be run.
    :param tasks: the selected top-level task names.
    :param origin: what asked for the isolation, for the diagnostics.
    :raises WorkspaceError: a selector names no top-level task, or one whose
        tool has no working directory to separate.
    """
    by_name = {t.name: t for t in conduit.tasks}
    for name in tasks:
        target = by_name.get(name)
        if target is None:
            close = difflib.get_close_matches(name, sorted(by_name), n=1)
            hint = f" — did you mean {close[0]!r}?" if close else ""
            raise WorkspaceError(
                f"{origin}: conduit {conduit.name!r} has no task {name!r}{hint}; "
                f"its top-level tasks are {sorted(by_name)}",
                code=1,
            )
        if not target.tool.startswith(ISOLATABLE_TOOL_PREFIXES):
            raise WorkspaceError(
                f"{origin}: task {name!r} runs {target.tool!r}, which has no "
                "working directory of its own — only agent tasks and "
                "tool:bash tasks can be given a separate checkout",
                code=1,
            )


def _git(args: list[str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
    """Run one ``git`` command, turning a missing Git into a usable message.

    :param args: the arguments after ``git``.
    :param cwd: the directory to run in.
    :returns: the completed process, successful or not.
    :raises WorkspaceError: Git is not installed, or ``cwd`` is gone.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise WorkspaceError(
            "--worktree needs Git on PATH, and 'git' was not found; install "
            "Git or drop --worktree to run every task in one directory",
            code=1,
        ) from exc
    except OSError as exc:
        raise WorkspaceError(
            f"--worktree cannot run Git in {cwd}: {exc}", code=1
        ) from exc


def resolve_source_repo(working_dir: Path | str | None) -> SourceRepo:
    """Return the checkout the worktrees will be cut from, or refuse.

    Resolved from the run's effective working directory, so a run started in a
    subdirectory — or inside an already-linked worktree — uses the checkout it
    is actually in. The source branch, index and files are never touched.

    Tracked changes are refused: a worktree carries the pinned commit, not the
    edits sitting in the source, and starting agents against a base that
    silently omits your work in progress is worse than saying so. Untracked and
    ignored files (``node_modules``, ``.env``, a virtualenv) are *not* copied
    either, but they cannot be pinned, so they are only reported, never a
    refusal.

    :param working_dir: the run's working directory; ``None`` means the
        process's current directory.
    :returns: the resolved :class:`SourceRepo`.
    :raises WorkspaceError: there is no usable repository, no commit to base
        on, or the tracked source is dirty.
    """
    where = Path(working_dir) if working_dir is not None else Path.cwd()
    if not where.is_dir():
        raise WorkspaceError(
            f"--worktree: working directory {str(where)!r} does not exist",
            code=1,
        )
    top = _git(["rev-parse", "--show-toplevel"], where)
    if top.returncode != 0:
        raise WorkspaceError(
            f"--worktree: {str(where)!r} is not inside a Git repository "
            f"({top.stderr.strip() or 'git rev-parse failed'}) — a separate "
            "checkout per task needs one to copy from",
            code=1,
        )
    root = Path(top.stdout.strip())
    head = _git(["rev-parse", "HEAD"], root)
    if head.returncode != 0:
        raise WorkspaceError(
            f"--worktree: {str(root)!r} has no commit to base a worktree on "
            f"({head.stderr.strip() or 'git rev-parse HEAD failed'}); commit "
            "something first",
            code=1,
        )
    dirty = _git(["status", "--porcelain", "--untracked-files=no"], root)
    if dirty.returncode != 0:
        raise WorkspaceError(
            f"--worktree: cannot read the state of {str(root)!r} "
            f"({dirty.stderr.strip() or 'git status failed'})",
            code=1,
        )
    changed = [line[3:] for line in dirty.stdout.splitlines() if line.strip()]
    if changed:
        shown = ", ".join(changed[:5]) + (" …" if len(changed) > 5 else "")
        raise WorkspaceError(
            f"--worktree: {str(root)!r} has uncommitted changes to tracked "
            f"files ({shown}); a worktree is cut from a commit, so those edits "
            "would not be in it — commit or stash them first",
            code=1,
        )
    return SourceRepo(root=root, head=head.stdout.strip())


def create_worktrees(
    source: SourceRepo, dest_root: Path, tasks: Iterable[str]
) -> Workspaces:
    """Create one detached worktree per task and return the saved mapping.

    Every worktree is cut from the same pinned commit, so the workers start
    from an identical base and their results are comparable. Detached on
    purpose: no branch is created, moved or checked out in the source.

    A failure part-way leaves the worktrees already created exactly where they
    are — an agent's checkout is the user's, and deleting it to tidy up a
    failed launch is the one thing this must never do.

    :param source: the checkout and commit to cut from.
    :param dest_root: directory to create the worktrees under; created if
        missing.
    :param tasks: the selected task names, in order.
    :returns: the :class:`Workspaces` record to persist.
    :raises WorkspaceError: a destination is taken or Git refused; the message
        names every worktree that was already created.
    """
    names = list(tasks)
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(
            f"--worktree: cannot create {str(dest_root)!r}: {exc}", code=1
        ) from exc
    made: dict[str, str] = {}
    for name in names:
        dest = dest_root / name
        # `git worktree add` refuses a non-empty destination, but an empty
        # directory or a dangling symlink it would happily take over — and
        # either can belong to someone else. Own only what we create.
        if dest.exists() or dest.is_symlink():
            raise WorkspaceError(
                _partial(
                    f"--worktree: {str(dest)!r} already exists, so task "
                    f"{name!r} was not given a checkout",
                    made,
                ),
                code=1,
            )
        done = _git(
            ["worktree", "add", "--detach", str(dest), source.head], source.root
        )
        if done.returncode != 0:
            raise WorkspaceError(
                _partial(
                    f"--worktree: Git could not create a checkout for task "
                    f"{name!r} at {str(dest)!r} "
                    f"({done.stderr.strip() or done.stdout.strip()})",
                    made,
                ),
                code=1,
            )
        made[name] = str(dest.resolve())
    return Workspaces(source=str(source.root), base=source.head, paths=made)


def _partial(message: str, made: Mapping[str, str]) -> str:
    """Append what was already created to a setup-failure message.

    :param message: the failure itself.
    :param made: the worktrees created before it, task name to path.
    :returns: the message, plus where the retained checkouts are.
    """
    if not made:
        return f"{message}; no checkout was created"
    kept = "; ".join(f"{task} -> {path}" for task, path in made.items())
    return (
        f"{message}. The checkouts already created are kept, not deleted: "
        f"{kept}. Remove them yourself (git worktree remove) once you have "
        "read anything you want from them"
    )


def verify_worktrees(saved: Workspaces, tasks: Iterable[str]) -> None:
    """Refuse a resume whose recorded checkouts are gone or not the same ones.

    Asked only about the work the resume will actually run: a task whose result
    is already saved is not started again, so its checkout is a record of where
    the edits went, not something this run needs back.

    Substituting a fresh checkout would silently discard a failed worker's
    partial edits — the exact thing a resume is for — so a missing or foreign
    directory stops the run instead.

    :param saved: the workspaces recorded by the run being resumed.
    :param tasks: the task names this resume will execute.
    :raises WorkspaceError: a required checkout is missing, is not a worktree,
        or belongs to a different repository.
    """
    wanted = [t for t in tasks if t in saved.paths]
    for name in wanted:
        path = Path(saved.paths[name])
        if not path.is_dir():
            raise WorkspaceError(
                f"task {name!r} has to continue in {str(path)!r}, and that "
                "directory is gone; its unfinished edits cannot be recovered "
                "and a fresh checkout would silently replace them — start a "
                "new run with `atelier run --again <flow_id>` if that is what "
                "you want",
                code=1,
            )
        top = _git(["rev-parse", "--show-toplevel"], path)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != path.resolve():
            raise WorkspaceError(
                f"task {name!r} has to continue in {str(path)!r}, and that is "
                "no longer the root of a Git checkout "
                f"({top.stderr.strip() or 'git rev-parse failed'}); refusing "
                "to run an agent somewhere other than where its work is",
                code=1,
            )
        common = _git(["rev-parse", "--git-common-dir"], path)
        expected = _git(["rev-parse", "--git-common-dir"], saved.source)
        if common.returncode != 0 or expected.returncode != 0:
            raise WorkspaceError(
                f"task {name!r} has to continue in {str(path)!r}, and its link "
                f"to {saved.source!r} cannot be read "
                f"({(common.stderr or expected.stderr).strip()})",
                code=1,
            )
        if (
            (path / common.stdout.strip()).resolve()
            != (Path(saved.source) / expected.stdout.strip()).resolve()
        ):
            raise WorkspaceError(
                f"task {name!r} has to continue in {str(path)!r}, and that "
                f"checkout now belongs to a different repository than the "
                f"{saved.source!r} this run recorded; refusing to continue "
                "against work that is not the same",
                code=1,
            )


def workspace_note(refs: Iterable[str], saved: Workspaces | None) -> str:
    """Return the block telling an agent where the results it quotes were made.

    A downstream step is handed the *text* an upstream worker produced, which
    says nothing about the checkout the work actually landed in. Without this a
    synthesis cannot open either candidate, and the paths would have to be
    pasted into every recipe by hand.

    Appended rather than substituted, for the same reason the agent-provenance
    block is: the user's prompt text is theirs. It appears only when a quoted
    task had a checkout of its own, so an ordinary shared-directory run reads
    exactly as it did.

    :param refs: the task names whose output this prompt quotes.
    :param saved: the run's recorded checkouts, or ``None``.
    :returns: the block to append, or ``""`` when there is nothing to say.
    """
    if saved is None:
        return ""
    named = sorted(set(refs) & set(saved.paths))
    if not named:
        return ""
    lines = "\n".join(
        f"{task} worked in {saved.paths[task]}" for task in named
    )
    return (
        "\n--- BEGIN WORKSPACE PROVENANCE (authoritative) ---\n"
        f"{lines}\n"
        f"Each is its own Git checkout of {saved.source} at commit "
        f"{saved.base}. Nothing has been merged: open a directory to read what "
        "that worker actually changed.\n"
        "--- END WORKSPACE PROVENANCE (authoritative) ---\n"
    )


def directory_size(path: Path) -> int:
    """Return the bytes used by the files under ``path``.

    Apparent size of regular files only, following no symlinks — enough to
    report what a checkout costs without pretending to know the filesystem's
    block accounting or what the shared object store already held.

    :param path: the directory to measure.
    :returns: total size in bytes; 0 when the path is gone.
    """
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total
