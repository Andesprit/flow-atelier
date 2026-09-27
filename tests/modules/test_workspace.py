"""Lifecycle of the per-task Git worktrees, against real repositories.

Nothing here is mocked: every assertion is made against directories `git`
actually created, in throwaway repositories under ``tmp_path``. The ones worth
reading twice are the refusals — a worktree that is missing, foreign or already
someone else's is where a careless implementation quietly destroys work.
"""
from __future__ import annotations

import subprocess

import pytest

from flow_atelier.modules.workspace import (
    WorkspaceError,
    check_selectors,
    create_worktrees,
    directory_size,
    parse_worktree_selectors,
    resolve_source_repo,
    verify_worktrees,
)
from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.progress import Workspaces

RECIPE = {
    "name": "pair",
    "description": "two writers and an approval",
    "tasks": [
        {
            "name": "writer_a",
            "description": "a",
            "task": "a",
            "tool": "harness:claude-code",
            "depends_on": [],
        },
        {
            "name": "writer_b",
            "description": "b",
            "task": "b",
            "tool": "tool:bash",
            "depends_on": [],
        },
        {
            "name": "sign_off",
            "description": "human",
            "task": "ok?",
            "tool": "tool:hitl",
            "depends_on": ["writer_a"],
        },
    ],
}


def git(*args: str, cwd) -> str:
    """Run one git command in ``cwd``, asserting it succeeded.

    :param args: the arguments after ``git``.
    :param cwd: the directory to run in.
    :returns: stdout, stripped.
    """
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A committed repository whose path contains a space.

    :param tmp_path: pytest temp directory fixture.
    :returns: the repository root.
    """
    root = tmp_path / "my repo"
    root.mkdir()
    git("init", "-q", ".", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "T", cwd=root)
    (root / "NOTES.md").write_text("base\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def conduit():
    """The recipe the selector checks are made against.

    :returns: the parsed :class:`Conduit`.
    """
    return Conduit.model_validate(RECIPE)


# ----------------------------------------------------------------- selectors


def test_selectors_keep_order_and_reject_repeats():
    assert parse_worktree_selectors(["writer_b", "writer_a"]) == [
        "writer_b",
        "writer_a",
    ]
    assert parse_worktree_selectors(None) == []
    with pytest.raises(WorkspaceError) as empty:
        parse_worktree_selectors(["  "])
    assert empty.value.code == 2
    with pytest.raises(WorkspaceError, match="twice"):
        parse_worktree_selectors(["writer_a", "writer_a"])


def test_a_path_is_not_accepted_as_a_selector():
    """The product owns the directory, so two runs cannot share one."""
    with pytest.raises(WorkspaceError, match="task name only"):
        parse_worktree_selectors(["writer_a=/tmp/mine"])


def test_unknown_task_is_refused_with_the_recipe_s_own_names(conduit):
    with pytest.raises(WorkspaceError) as exc:
        check_selectors(conduit, ["writer_c"])
    assert exc.value.code == 1
    assert "no task 'writer_c'" in str(exc.value)
    assert "['sign_off', 'writer_a', 'writer_b']" in str(exc.value)


def test_a_human_step_has_no_checkout_to_give(conduit):
    with pytest.raises(WorkspaceError, match="no working directory of its own"):
        check_selectors(conduit, ["sign_off"])


def test_agent_and_bash_tasks_are_both_isolatable(conduit):
    check_selectors(conduit, ["writer_a", "writer_b"])


# -------------------------------------------------------------------- source


def test_source_is_the_checkout_the_run_starts_in(repo):
    source = resolve_source_repo(repo / "src")
    assert source.root == repo
    assert source.head == git("rev-parse", "HEAD", cwd=repo)


def test_a_linked_worktree_is_a_usable_source(repo, tmp_path):
    """Running inside someone else's worktree bases on *that* checkout."""
    linked = tmp_path / "linked"
    git("worktree", "add", "--detach", str(linked), "HEAD", cwd=repo)
    (linked / "NOTES.md").write_text("linked\n", encoding="utf-8")
    git("commit", "-qam", "linked", cwd=linked)
    source = resolve_source_repo(linked)
    assert source.root == linked
    assert source.head == git("rev-parse", "HEAD", cwd=linked)
    assert source.head != git("rev-parse", "HEAD", cwd=repo)


def test_outside_a_repository_is_refused(tmp_path):
    with pytest.raises(WorkspaceError) as exc:
        resolve_source_repo(tmp_path)
    assert exc.value.code == 1
    assert "not inside a Git repository" in str(exc.value)


def test_a_repository_with_no_commit_is_refused(tmp_path):
    empty = tmp_path / "fresh"
    empty.mkdir()
    git("init", "-q", ".", cwd=empty)
    with pytest.raises(WorkspaceError, match="no commit to base a worktree on"):
        resolve_source_repo(empty)


def test_uncommitted_tracked_changes_are_refused_by_name(repo):
    (repo / "NOTES.md").write_text("edited\n", encoding="utf-8")
    with pytest.raises(WorkspaceError) as exc:
        resolve_source_repo(repo)
    assert "NOTES.md" in str(exc.value)
    assert "would not be in it" in str(exc.value)


def test_untracked_files_do_not_block_a_run(repo):
    """They are not copied either, but they cannot be pinned to a commit."""
    (repo / "scratch.txt").write_text("ignore me\n", encoding="utf-8")
    assert resolve_source_repo(repo).root == repo


def test_a_missing_working_directory_is_refused(tmp_path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        resolve_source_repo(tmp_path / "gone")


# ------------------------------------------------------------------- create


def test_each_task_gets_its_own_detached_checkout(repo, tmp_path):
    source = resolve_source_repo(repo)
    saved = create_worktrees(source, tmp_path / "ws", ["writer_a", "writer_b"])

    assert sorted(saved.paths) == ["writer_a", "writer_b"]
    assert saved.base == source.head
    assert saved.source == str(repo)
    for name, path in saved.paths.items():
        assert (tmp_path / "ws" / name).samefile(path)
        assert (tmp_path / "ws" / name / "NOTES.md").read_text() == "base\n"
        assert git("rev-parse", "HEAD", cwd=path) == source.head
        # Detached: no branch was created, moved or checked out anywhere.
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) == "HEAD"
    assert git("status", "--porcelain", cwd=repo) == ""


def test_edits_in_one_checkout_do_not_reach_the_others(repo, tmp_path):
    source = resolve_source_repo(repo)
    saved = create_worktrees(source, tmp_path / "ws", ["writer_a", "writer_b"])
    a, b = saved.paths["writer_a"], saved.paths["writer_b"]
    (tmp_path / "ws" / "writer_a" / "NOTES.md").write_text("A\n", encoding="utf-8")

    assert (tmp_path / "ws" / "writer_b" / "NOTES.md").read_text() == "base\n"
    assert (repo / "NOTES.md").read_text() == "base\n"
    assert git("status", "--porcelain", cwd=a) == "M NOTES.md"
    assert git("status", "--porcelain", cwd=b) == ""


def test_a_taken_destination_stops_setup_and_keeps_what_was_built(repo, tmp_path):
    source = resolve_source_repo(repo)
    (tmp_path / "ws" / "writer_b").mkdir(parents=True)
    with pytest.raises(WorkspaceError) as exc:
        create_worktrees(source, tmp_path / "ws", ["writer_a", "writer_b"])

    message = str(exc.value)
    assert "already exists" in message
    assert "are kept, not deleted" in message
    # The first one was built before the second was refused, and it is still
    # there — with its path in the message, so the user can go and read it.
    assert str(tmp_path / "ws" / "writer_a") in message
    assert (tmp_path / "ws" / "writer_a" / "NOTES.md").exists()


def test_the_first_failure_says_nothing_was_created(repo, tmp_path):
    source = resolve_source_repo(repo)
    (tmp_path / "ws" / "writer_a").mkdir(parents=True)
    with pytest.raises(WorkspaceError, match="no checkout was created"):
        create_worktrees(source, tmp_path / "ws", ["writer_a"])


def test_a_dangling_symlink_destination_is_not_taken_over(repo, tmp_path):
    source = resolve_source_repo(repo)
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "writer_a").symlink_to(tmp_path / "nowhere")
    with pytest.raises(WorkspaceError, match="already exists"):
        create_worktrees(source, tmp_path / "ws", ["writer_a"])
    assert (tmp_path / "ws" / "writer_a").is_symlink()


def test_two_runs_of_the_same_task_get_different_directories(repo, tmp_path):
    source = resolve_source_repo(repo)
    first = create_worktrees(source, tmp_path / "ws" / "run-1", ["writer_a"])
    second = create_worktrees(source, tmp_path / "ws" / "run-2", ["writer_a"])
    assert first.paths["writer_a"] != second.paths["writer_a"]


# ------------------------------------------------------------------- verify


def test_verify_passes_for_the_checkouts_it_recorded(repo, tmp_path):
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    verify_worktrees(saved, ["writer_a", "writer_b"], owned_root=tmp_path / "ws")


def test_a_deleted_checkout_is_never_silently_replaced(repo, tmp_path):
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    git("worktree", "remove", "--force", saved.paths["writer_a"], cwd=repo)

    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(saved, ["writer_a"], owned_root=tmp_path / "ws")
    assert exc.value.code == 1
    assert "that directory is gone" in str(exc.value)
    assert "--again" in str(exc.value)


def test_a_finished_task_s_checkout_is_not_required(repo, tmp_path):
    """Its result is saved; the resume will not start it again."""
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    git("worktree", "remove", "--force", saved.paths["writer_a"], cwd=repo)
    verify_worktrees(saved, ["writer_b"], owned_root=tmp_path / "ws")


def test_a_plain_directory_in_its_place_is_refused(repo, tmp_path):
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    git("worktree", "remove", "--force", saved.paths["writer_a"], cwd=repo)
    (tmp_path / "ws" / "writer_a").mkdir()

    with pytest.raises(WorkspaceError, match="no longer the root of a Git checkout"):
        verify_worktrees(saved, ["writer_a"], owned_root=tmp_path / "ws")


def test_a_checkout_of_another_repository_is_refused(repo, tmp_path):
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    git("worktree", "remove", "--force", saved.paths["writer_a"], cwd=repo)
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", ".", cwd=other)
    git("config", "user.email", "t@example.com", cwd=other)
    git("config", "user.name", "T", cwd=other)
    (other / "NOTES.md").write_text("other\n", encoding="utf-8")
    git("add", "-A", cwd=other)
    git("commit", "-qm", "init", cwd=other)
    git("worktree", "add", "--detach", saved.paths["writer_a"], "HEAD", cwd=other)

    with pytest.raises(WorkspaceError, match="different repository"):
        verify_worktrees(saved, ["writer_a"], owned_root=tmp_path / "ws")


def test_a_record_that_lists_no_checkout_is_refused(tmp_path):
    """A run with nothing isolated records no Workspaces at all.

    So a record with an empty mapping is damaged, and reading it as "share one
    directory" would silently undo the isolation the run was asked for.
    """
    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(
            Workspaces(source="/nowhere", base="0" * 40),
            ["writer_a"],
            owned_root=tmp_path / "ws",
        )
    assert exc.value.code == 1
    assert "lists none of them" in str(exc.value)


def test_a_record_that_does_not_say_what_it_isolated_is_refused(tmp_path):
    """Without the selection there is nothing to measure the mapping against."""
    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(
            Workspaces(
                source="/nowhere", base="0" * 40, paths={"writer_a": "/w/writer_a"}
            ),
            ["writer_a"],
            owned_root=tmp_path / "ws",
        )
    assert exc.value.code == 1
    assert "does not say which tasks it isolated" in str(exc.value)


def test_a_record_missing_one_of_its_checkouts_is_refused(repo, tmp_path):
    """Losing an entry is the quiet failure: it reads as "never isolated".

    The task would then be run in the shared source checkout and overwrite the
    user's own files, so the run's own record of what it isolated is what the
    mapping has to match.
    """
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    gutted = saved.model_copy(
        update={"paths": {"writer_a": saved.paths["writer_a"]}}
    )

    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(gutted, ["writer_b"], owned_root=tmp_path / "ws")
    assert exc.value.code == 1
    assert "no longer says where writer_b worked" in str(exc.value)


def test_a_checkout_the_record_does_not_name_is_refused(repo, tmp_path):
    """The directories on disk are evidence the record cannot edit away.

    Dropping writer_b from ``selected`` as well as ``paths`` makes the record
    self-consistent, so only its own checkout still proves it was isolated.
    """
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    forged = saved.model_copy(
        update={
            "selected": ["writer_a"],
            "paths": {"writer_a": saved.paths["writer_a"]},
        }
    )

    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(forged, ["writer_b"], owned_root=tmp_path / "ws")
    assert exc.value.code == 1
    assert "no longer says where writer_b worked" in str(exc.value)
    assert repr(str(tmp_path / "ws")) in str(exc.value)


def test_a_run_that_isolated_only_some_tasks_is_complete(repo, tmp_path):
    """A smaller selection is a choice, not damage: the rest share a directory."""
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    verify_worktrees(saved, ["writer_a", "writer_b"], owned_root=tmp_path / "ws")
    assert saved.selected == ["writer_a"]


def test_a_record_without_a_source_or_base_is_refused(tmp_path):
    for damaged in (
        Workspaces(source="", base="0" * 40, paths={"writer_a": "/w/writer_a"}),
        Workspaces(source="/nowhere", base="  ", paths={"writer_a": "/w/writer_a"}),
    ):
        with pytest.raises(WorkspaceError, match="no source repository or base"):
            verify_worktrees(damaged, ["writer_a"], owned_root=tmp_path / "ws")


def test_the_source_checkout_in_place_of_a_task_is_refused(repo, tmp_path):
    """The user's own files are the first thing a redirected resume destroys."""
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    retargeted = saved.model_copy(update={"paths": {"writer_a": str(repo)}})

    with pytest.raises(WorkspaceError) as exc:
        verify_worktrees(retargeted, ["writer_a"], owned_root=tmp_path / "ws")
    assert exc.value.code == 1
    assert "not the checkout this run created for it" in str(exc.value)


def test_another_task_s_checkout_in_place_of_one_is_refused(repo, tmp_path):
    """Two tasks pointed at one directory is the overwriting this prevents."""
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    shared = saved.paths["writer_a"]
    retargeted = saved.model_copy(
        update={"paths": {"writer_a": shared, "writer_b": shared}}
    )

    with pytest.raises(WorkspaceError, match="not the checkout this run created"):
        verify_worktrees(retargeted, ["writer_b"], owned_root=tmp_path / "ws")


def test_a_completed_task_pointed_somewhere_else_is_refused_too(repo, tmp_path):
    """Its path is read for provenance even when the task is not re-run."""
    saved = create_worktrees(
        resolve_source_repo(repo), tmp_path / "ws", ["writer_a", "writer_b"]
    )
    retargeted = saved.model_copy(
        update={"paths": {**saved.paths, "writer_a": str(repo)}}
    )

    with pytest.raises(WorkspaceError, match="not the checkout this run created"):
        verify_worktrees(retargeted, ["writer_b"], owned_root=tmp_path / "ws")


# --------------------------------------------------------------------- cost


def test_directory_size_counts_the_files_it_can_read(repo, tmp_path):
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    size = directory_size(tmp_path / "ws" / "writer_a")
    assert size >= len("base\n") + len("print(1)\n")
    assert directory_size(tmp_path / "nowhere") == 0
    assert saved.paths["writer_a"]


def test_the_checkouts_do_not_show_up_in_the_source_s_status(repo):
    """They live inside the source's tree, so they must be ignored there."""
    create_worktrees(
        resolve_source_repo(repo), repo / ".atelier" / "workspaces" / "f1", ["writer_a"]
    )
    assert git("status", "--porcelain", "--untracked-files=all", cwd=repo) == ""


def test_a_link_the_platform_does_not_report_is_still_refused(repo, tmp_path, monkeypatch):
    """A Windows junction is not a symlink to is_symlink(); Git still tells.

    Simulated with a real symlink the check is told is not one, which is how a
    junction to the source checkout looks to everything but Git.
    """
    saved = create_worktrees(resolve_source_repo(repo), tmp_path / "ws", ["writer_a"])
    git("worktree", "remove", "--force", saved.paths["writer_a"], cwd=repo)
    (tmp_path / "ws" / "writer_a").symlink_to(repo, target_is_directory=True)
    monkeypatch.setattr(type(tmp_path), "is_symlink", lambda self: False)

    with pytest.raises(WorkspaceError, match="main checkout"):
        verify_worktrees(saved, ["writer_a"], owned_root=tmp_path / "ws")
