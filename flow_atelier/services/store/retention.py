"""Expire finished global run trees. Legacy stores and project workspaces have separate owners."""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from flow_atelier.schemas.progress import FlowStatus, Progress
from flow_atelier.services.store.claims import expire_claim
from flow_atelier.services.store.events import RunJournal, RunRecordError, file_lock

logger = logging.getLogger(__name__)
DAY_SECONDS = 24 * 60 * 60


def _now() -> datetime:
    return datetime.now(UTC)


def _expired(journal: RunJournal, now: datetime, days: int) -> bool:
    progress = Progress.model_validate(journal.latest("progress"))
    return bool(
        progress.status != FlowStatus.running
        and not progress.pending_ask
        and progress.finished_at
        and (now - datetime.fromisoformat(progress.finished_at)).days >= days
    )


def _remove_expired(path: Path) -> None:
    # A tombstone survives a crash between retiring the journal and removing its blobs.
    journal = RunJournal(path)
    journal.header()
    if journal.side.is_symlink() or path.with_suffix(".jsonl").exists():
        raise RunRecordError(f"conflicting expired record: {path}")
    expire_claim(path.parents[2], path.stem)
    if journal.side.exists():
        shutil.rmtree(journal.side)
    path.unlink()


def _expire_tree(tree: list[RunJournal], now: datetime, days: int) -> None:
    if not all(_expired(journal, now, days) for journal in tree):
        return
    retired = []
    with ExitStack() as locks:
        for journal in tree:
            if journal.side.is_symlink():
                raise RunRecordError(f"linked run directory: {journal.side}")
            journal.side.mkdir(exist_ok=True)
            for name in (".writer.lock", ".ask.lock", ".notify.lock"):
                path = journal.side / name
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise RunRecordError(f"invalid run lease: {path}")
                if not locks.enter_context(file_lock(path, wait=False)):
                    return
        # A resume may have changed the last finish time while we were acquiring leases.
        if not all(_expired(journal, now, days) for journal in tree):
            return
        for journal in reversed(tree):
            path = journal.path.with_suffix(".expired")
            if path.exists():
                raise RunRecordError(f"conflicting expired record: {path}")
            retired.append(journal.path.rename(path))
    # Windows will not remove an open lock file.
    for path in retired:
        _remove_expired(path)


def cleanup_lock(root: Path) -> AbstractContextManager[bool]:
    """Serialize the tree scan with publication of migrated trees."""
    lease = root / ".cleanup.lock"
    if lease.is_symlink() or (lease.exists() and not lease.is_file()):
        raise RunRecordError(f"invalid cleanup lease: {lease}")
    return file_lock(lease, wait=False)


def cleanup_runs(home: Path, days: int) -> None:
    """Keep a whole tree until every member has finished outside the retention window."""
    if days < 0:
        raise ValueError("cleanup period must be non-negative")
    root = home / "projects"
    if root.is_symlink() or not root.is_dir():
        return
    now = _now()
    with cleanup_lock(root) as acquired:
        if not acquired:
            return
        children: dict[str | None, list[RunJournal]] = defaultdict(list)
        seen: set[str] = set()
        # Collect before deleting. An unreadable header might belong to an otherwise eligible tree.
        for folder in root.iterdir():
            if folder.is_symlink() or not folder.is_dir() or folder.name.startswith("."):
                continue
            for path in folder.glob("*.expired"):
                if not path.is_symlink():
                    _remove_expired(path)
            for path in folder.glob("*.jsonl"):
                if path.is_symlink() or path.stem in seen:
                    raise RunRecordError(f"ambiguous or linked run journal: {path}")
                seen.add(path.stem)
                journal = RunJournal(path)
                header = journal.header()
                # An interrupted publish may still be missing children.
                if (
                    header["parent_flow_id"] is None
                    and "legacy_workspace_root" in header
                    and (Path(header["store"]) / "flows" / path.stem).exists()
                ):
                    continue
                children[header["parent_flow_id"]].append(journal)
        for parent in children[None]:
            tree = [parent]
            for journal in tree:
                tree.extend(children.get(journal.path.stem, ()))
            try:
                _expire_tree(tree, now, days)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Could not expire run tree %s: %s", parent.path, exc)


@asynccontextmanager
async def daily_cleanup(home: Path, days: int) -> AsyncIterator[None]:
    async def sweep() -> None:
        try:
            await asyncio.to_thread(cleanup_runs, home, days)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not clean run history in %s: %s", home, exc)

    await sweep()
    stopped = asyncio.Event()

    async def repeat() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), DAY_SECONDS)
            except TimeoutError:
                await sweep()

    task = asyncio.create_task(repeat())
    try:
        yield
    finally:
        stopped.set()
        await task
