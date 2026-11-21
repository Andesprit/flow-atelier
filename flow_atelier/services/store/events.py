"""Version 1 run journal. Only newline-terminated events are committed."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from flow_atelier.schemas.flow import parse_flow_id


def project_folder(project: Path) -> str:
    return "".join(c if c.isalnum() else "-" for c in str(project.resolve()))


class RunJournal:
    def __init__(self, path: Path):
        self.path = path
        self.side = path.with_suffix("")

    def _rows(self, stream: BinaryIO) -> Iterator[tuple[str, dict[str, Any]]]:
        for number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                return
            try:
                row = json.loads(line)
                if row["version"] != 1:
                    raise ValueError("unsupported event version")
                kind, data = row["type"], row["data"]
                if number == 1:
                    if (
                        kind != "created"
                        or data["flow_id"] != self.path.stem
                        or data["conduit"] != parse_flow_id(self.path.stem)[0]
                        or not Path(data["project"]).is_absolute()
                        or project_folder(Path(data["project"])) != self.path.parent.name
                        or data["steps"] != "steps.jsonl"
                    ):
                        raise ValueError("invalid run header")
                elif kind not in {"progress", "outputs", "input", "log", "sessions"}:
                    raise ValueError(f"unknown event type {kind!r}")
                if not isinstance(data, dict):
                    raise ValueError("expected event object")
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"{self.path} line {number}: {exc}") from exc
            yield kind, data

    def rows(self) -> Iterator[tuple[str, dict[str, Any]]]:
        with self.path.open("rb") as stream:
            yield from self._rows(stream)

    def header(self) -> dict[str, Any]:
        with self.path.open("rb") as stream:
            first = next(self._rows(stream), None)
        if first is None:
            raise ValueError(f"{self.path}: missing run header")
        return first[1]

    def latest(self, kind: str) -> dict[str, Any]:
        value = {}
        for event, data in self.rows():
            if event == "created":
                value = data.get(kind, {})
            elif event == kind:
                value = data
        return value

    def inputs(self) -> dict[str, Any]:
        result = {}
        for kind, data in self.rows():
            if kind == "created":
                result.update(data["inputs"])
            elif kind == "input":
                result.update(data)
        return result

    def unpack(self, data: dict[str, Any]) -> dict[str, Any]:
        result = data["values"].copy()
        for key, relative in data["files"].items():
            path = Path(relative)
            if (
                len(path.parts) != 3
                or path.parts[0] != "outputs"
                or path.parts[1] == ".."
                or not path.name.isdecimal()
                or not (self.side / path).resolve().is_relative_to(self.side.resolve())
            ):
                raise ValueError(f"{self.path}: invalid output reference {relative!r}")
            result[key] = (self.side / path).read_bytes().decode("utf-8")
        return result

    def outputs(self) -> dict[str, Any]:
        saved = self.latest("outputs")
        return self.unpack(saved) if saved else {}

    def logs(self) -> Iterator[dict[str, Any]]:
        for kind, data in self.rows():
            if kind == "log":
                yield self.unpack(data)
