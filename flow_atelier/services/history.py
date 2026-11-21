"""Read selected local session journals as data. Source IDs never accept arbitrary paths."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path


def _sources() -> Iterator[tuple[str, Path, Path]]:
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex").resolve()
    claude = home / ".claude/projects"
    patterns = [
        ("codex", codex, "sessions/*/*/*/rollout-*.jsonl"),
        ("codex", codex, "archived_sessions/rollout-*.jsonl"),
        ("codex", codex, "archived_sessions/rollout-*.jsonl.zst"),
        ("claude-code", claude, "*/*.jsonl"),
        ("claude-code", claude, "*/*/subagents/*.jsonl"),
    ]
    for provider, root, pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.resolve() == path and path.is_file():
                yield provider, path, root
    for name in ("antigravity", "antigravity-cli"):
        root = home / ".gemini" / name
        found = set()
        for directory in sorted((root / "brain").glob("*")):
            if directory.resolve() == directory and directory.is_dir():
                path = directory / ".system_generated/logs/transcript.jsonl"
                if path.resolve() == path:
                    found.add(directory.name)
                    yield name, path if path.is_file() else directory, root
        for path in sorted((root / "conversations").glob("*")):
            if (
                path.suffix in {".pb", ".db", ".sqlite", ".sqlite3"}
                and path.stem not in found
                and path.resolve() == path
                and path.is_file()
            ):
                yield name, path, root


def _content(value):
    if isinstance(value, list):
        blocks = [block for block in value if isinstance(block, dict)]
        if all(block.get("type") in ("text", "input_text", "output_text") for block in blocks):
            return "".join(b["text"] for b in blocks if isinstance(b.get("text"), str))
        return blocks
    return value if isinstance(value, str) else None


def _message(provider, row):
    kind = row.get("type")
    if not isinstance(kind, str):
        return None, None
    if provider == "codex":
        data = row.get("payload")
        if not isinstance(data, dict):
            return None, None
        if kind == "response_item" and data.get("type") == "message":
            return data.get("role"), _content(data.get("content"))
        roles = {"user_message": "user", "agent_message": "assistant"}
        if kind == "event_msg" and isinstance(data.get("type"), str):
            return roles.get(data.get("type")), _content(data.get("message"))
    elif provider == "claude-code":
        data = row.get("message")
        if isinstance(data, dict):
            return kind, _content(data.get("content"))
    else:
        roles = {
            "USER_INPUT": "user",
            "PLANNER_RESPONSE": "assistant",
            "user": "user",
            "assistant": "assistant",
        }
        return roles.get(kind), _content(row.get("content"))
    return None, None


def _read(provider: str, path: Path, root: Path, selected: bool) -> dict:
    subagent = path.parent.name == "subagents"
    session = path.parent.parent.name if subagent else path.stem
    if provider.startswith("antigravity") and path.name == "transcript.jsonl":
        session = path.relative_to(root).parts[1]
    info = dict(
        provider=provider,
        session=session,
        source=str(path),
        subagent=subagent,
        id=hashlib.sha256(os.fsencode(path)).hexdigest(),
        project=None,
        date=None,
    )
    if selected:
        info["messages"] = []
    errors = (ValueError, OSError, EOFError)
    try:
        opener = open
        if path.suffix == ".zst":
            if sys.version_info < (3, 14):
                raise ValueError("Skipped compressed archive. Reading it requires Python 3.14.")
            from compression import zstd

            opener = zstd.open
            errors += (zstd.ZstdError,)
        elif path.suffix != ".jsonl":
            raise ValueError("Transcript unavailable: no readable JSONL")
        seen = Counter()
        previous = None
        with opener(path, "rb") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    if not line.endswith(b"\n"):
                        info["warning"] = f"Ignored incomplete tail at line {number}"
                        break
                    raise ValueError(f"Invalid JSON at line {number}") from exc
                if not isinstance(row, dict):
                    continue
                meta = row.get("payload") if row.get("type") == "session_meta" else row
                if isinstance(meta, dict):
                    info["project"] = info["project"] or meta.get("cwd") or meta.get("project")
                    record_id = meta.get("id") if row.get("type") == "session_meta" else None
                    info["session"] = row.get("sessionId") or record_id or info["session"]
                stamp = row.get("timestamp") or row.get("created_at")
                info["date"] = info["date"] or stamp
                role, content = _message(provider, row)
                if not selected:
                    if role in ("assistant", "user") or row.get("type") == "session_meta":
                        break
                    continue
                if role not in ("assistant", "user") or not content:
                    continue
                if provider == "codex" and isinstance(content, str):
                    kind = row["type"]
                    other = "response_item" if kind == "event_msg" else "event_msg"
                    if previous != (role, content):
                        seen.clear()
                        previous = (role, content)
                    seen[kind] += 1
                    if seen[kind] <= seen[other]:
                        continue
                info["messages"].append(dict(role=role, content=content, date=stamp))
    except errors as exc:
        info["error"] = str(exc)
    return info


def read_history(source_id: str | None = None) -> list[dict] | dict:
    """Index metadata or read one previously discovered transcript."""
    rows = []
    for provider, path, root in _sources():
        if source_id is None:
            rows.append(_read(provider, path, root, False))
        elif hashlib.sha256(os.fsencode(path)).hexdigest() == source_id:
            return _read(provider, path, root, True)
    if source_id is not None:
        raise ValueError("Unknown session source ID")
    return rows
