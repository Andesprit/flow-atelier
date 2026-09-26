"""Abstract store interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal

from flow_atelier.schemas.conduit import Conduit
from flow_atelier.schemas.log import LogEntry, StepRecord
from flow_atelier.schemas.progress import Progress

ConduitSource = Literal["project", "global"]


class StoreBase(ABC):
    """File-I/O abstraction. Manages conduits, flows, logs, progress."""

    # --- conduits ---
    @abstractmethod
    def read_conduit(self, name: str) -> Conduit:
        """Load conduit ``name`` from the store.

        :param name: conduit name
        """
        ...

    @abstractmethod
    def list_conduits(self) -> list[str]:
        """Return all visible conduit names."""
        ...

    @abstractmethod
    def list_conduits_with_source(self) -> list[tuple[str, ConduitSource]]:
        """Return ``(name, source)`` pairs. Project shadows global on collision."""
        ...

    @abstractmethod
    def conduit_dir(self, name: str) -> Path:
        """Return the directory conduit ``name`` loads from (project then global).

        :param name: conduit name
        :raises FileNotFoundError: if not found in either store
        """
        ...

    @abstractmethod
    def write_conduit(self, conduit: Conduit) -> None:
        """Persist ``conduit`` to the project store, overwriting if present.

        :param conduit: validated conduit to persist
        """
        ...

    @abstractmethod
    def delete_conduit(self, name: str) -> bool:
        """Delete a project-level conduit. Returns False if it didn't exist.

        :param name: conduit name
        """
        ...

    # --- flows ---
    @abstractmethod
    def create_flow(
        self,
        conduit_name: str,
        inputs: dict[str, Any],
        parent_flow_id: str | None = None,
        *,
        flow_id: str | None = None,
    ) -> str:
        """Returns the new flow_id.

        :param conduit_name: conduit being run
        :param inputs: initial input map persisted with the flow
        :param parent_flow_id: optional parent flow for nested runs
        :param flow_id: optional pre-generated flow id; when ``None`` the
            store generates one via :func:`new_flow_id`
        """

    @abstractmethod
    def list_flows(self, conduit_name: str | None = None) -> list[str]:
        """List top-level flow ids, optionally filtered by conduit.

        :param conduit_name: when set, only flows for this conduit are returned
        """
        ...

    @abstractmethod
    def list_child_flows(self, parent_flow_id: str) -> list[str]:
        """List flow ids that are children of ``parent_flow_id``, oldest first.

        :param parent_flow_id: parent flow identifier
        :returns: child flow ids in the order they last started (a resumed
            child counts from its resume), so the last one is the most recent
        """
        ...

    @abstractmethod
    def delete_flow(self, flow_id: str) -> bool:
        """Delete a flow directory and its nested children. Returns False if absent.

        :param flow_id: flow identifier
        :returns: True if it existed and was deleted, False otherwise
        """
        ...

    # --- logs ---
    @abstractmethod
    async def append_log(self, flow_id: str, entry: LogEntry) -> None:
        """Append a log entry for ``flow_id``.

        :param flow_id: flow identifier
        :param entry: log entry to append
        """
        ...

    @abstractmethod
    def read_logs(self, flow_id: str) -> list[LogEntry]:
        """Return all log entries for ``flow_id`` in append order.

        :param flow_id: flow identifier
        :returns: list of :class:`LogEntry` — empty if the log is missing or empty
        """
        ...

    # --- live steps ---
    @abstractmethod
    async def append_step(self, flow_id: str, record: StepRecord) -> None:
        """Append one live step record for ``flow_id``.

        Written as the step happens, unlike :meth:`append_log` which only
        runs once a task returns.

        :param flow_id: flow identifier
        :param record: the step record to append
        """
        ...

    @abstractmethod
    def read_steps(
        self, flow_id: str, offset: int = 0
    ) -> tuple[list[StepRecord], int]:
        """Return live step records for ``flow_id`` in append order.

        :param flow_id: flow identifier
        :param offset: opaque resume position from a previous call, for
            pollers that have already consumed the records before it
        :returns: tuple of the records (empty if none were recorded) and the
            position to pass as ``offset`` on the next call
        """
        ...

    # --- progress ---
    # State writes are deliberately synchronous (unlike append_log): the
    # payloads are tiny and crash-resume relies on ordered persistence.
    @abstractmethod
    def write_progress(self, flow_id: str, progress: Progress) -> None:
        """Persist the progress snapshot for ``flow_id``.

        :param flow_id: flow identifier
        :param progress: progress snapshot to persist
        """
        ...

    @abstractmethod
    def read_progress(self, flow_id: str) -> Progress:
        """Return the ``Progress`` snapshot for ``flow_id``.

        :param flow_id: flow identifier
        """
        ...

    # --- outputs.yaml ---

    @abstractmethod
    def read_outputs(self, flow_id: str) -> dict[str, Any]:
        """Return the per-task output map for ``flow_id`` (empty if missing).

        :param flow_id: flow identifier
        :returns: mapping of task name to output string
        """
        ...

    @abstractmethod
    def write_outputs(self, flow_id: str, outputs: dict[str, Any]) -> None:
        """Persist the per-task output map for ``flow_id``.

        :param flow_id: flow identifier
        :param outputs: mapping of task name to final output (``None`` for tasks
            that did not complete)
        """
        ...

    # --- input.yaml ---
    @abstractmethod
    def read_input(self, flow_id: str) -> dict[str, Any]:
        """Return the input map persisted for ``flow_id``.

        :param flow_id: flow identifier
        """
        ...

    @abstractmethod
    def append_input(self, flow_id: str, key: str, value: Any) -> None:
        """Set ``key=value`` in the flow's input map.

        :param flow_id: flow identifier
        :param key: input key to add or overwrite
        :param value: value to store under ``key``
        """
        ...
