import base64
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from flow_atelier.cli.main import app


def put(path, *records, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records) + tail, encoding="utf-8")


def history(*args):
    result = CliRunner().invoke(app, ["history", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_provider_sources_and_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    home = Path.home()
    codex = tmp_path / "codex/sessions/2026/01/01/rollout-test.jsonl"
    put(
        codex,
        {"type": "session_meta", "timestamp": "2026-01-01", "payload": {"cwd": "/repo"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "first"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "first"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "second"}],
            },
        },
        {},
        tail='{"type":',
    )
    claude = home / ".claude/projects/project/one.jsonl"
    put(
        claude,
        {"type": "user", "sessionId": "one", "cwd": "/repo", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
    )
    put(
        claude.parent / "one/subagents/agent-2.jsonl",
        {
            "type": "assistant",
            "sessionId": "one",
            "message": {"content": [{"type": "text", "text": "child"}]},
        },
    )
    for root in ("antigravity", "antigravity-cli"):
        put(
            home / f".gemini/{root}/brain/same/.system_generated/logs/transcript.jsonl",
            {"type": "USER_INPUT", "step_index": 1, "content": "q"},
            {"type": "PLANNER_RESPONSE", "created_at": "today", "content": "a"},
        )
    binary = home / ".gemini/antigravity-cli/conversations/binary.pb"
    binary.parent.mkdir(parents=True)
    binary.touch()
    broken = claude.with_name("bad.jsonl")
    broken.write_bytes(b"\xff\n")
    sessions = history()
    assert len(sessions) == 7
    assert all("messages" not in row for row in sessions)
    rows = {row["source"]: row for row in sessions}
    assert rows[str(broken)]["error"]
    assert "unavailable" in rows[str(binary)]["error"]
    assert rows[str(codex)]["project"] == "/repo"
    assert rows[str(codex)]["date"] == "2026-01-01"
    selected = history(rows[str(codex)]["id"])
    assert [(m["role"], m["content"]) for m in selected["messages"]] == [
        ("user", "first"),
        ("assistant", "second"),
    ]
    assert "tail" in selected["warning"]
    assert history(rows[str(claude)]["id"])["messages"][1]["content"][0]["name"] == "Read"
    sub = next(row for row in sessions if row.get("subagent"))
    assert sub["session"] == "one"
    assert history(sub["id"])["messages"][0]["content"] == "child"
    for row in sessions:
        if row["source"].endswith("transcript.jsonl"):
            assert [m["content"] for m in history(row["id"])["messages"]] == ["q", "a"]


def test_archive_and_source_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    archive = tmp_path / "archived_sessions/rollout-old.jsonl.zst"
    archive.parent.mkdir()
    archive.write_bytes(
        base64.b64decode(
            "KLUv/SBMFQIAZAN7InR5cGUiOiJldmVudF9tc2ciLCJwYXlsb2FkIjphZ2Vzc2FnZSIsIjoiYXJjaGl2ZSJ9fQoDAC10wMWGQjkB"
        )
    )
    outside = tmp_path / "private.jsonl"
    put(outside, {"secret": "must stay outside"})
    archive.with_name("rollout-link.jsonl").symlink_to(outside)
    rows = history()
    assert len(rows) == 1
    if sys.version_info < (3, 14):
        assert "3.14" in rows[0]["error"]
    else:
        assert history(rows[0]["id"])["messages"][0]["content"] == "archive"
    assert CliRunner().invoke(app, ["history", str(outside), "--json"]).exit_code != 0


def test_repeated_question_is_a_new_turn_after_a_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    put(
        tmp_path / "sessions/2026/01/01/rollout-repeat.jsonl",
        {"type": "event_msg", "payload": {"type": "user_message", "message": "again"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "again"},
        },
    )
    turns = history(history()[0]["id"])["messages"]
    assert [turn["content"] for turn in turns] == ["again", "ok", "again"]


def test_linked_provider_roots_and_custom_claude_home(tmp_path, monkeypatch):
    configured = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(configured))
    actual = tmp_path / "real-claude"
    session = actual / "projects/repo/session.jsonl"
    put(session, {"type": "user", "message": {"content": "hi"}})
    configured.symlink_to(actual, target_is_directory=True)
    outside = tmp_path / "private.jsonl"
    put(outside, {"type": "user", "message": {"content": "private"}})
    session.with_name("escape.jsonl").symlink_to(outside)
    gemini = tmp_path / "real-gemini"
    transcript = gemini / "antigravity/brain/id/.system_generated/logs/transcript.jsonl"
    put(transcript, {"type": "USER_INPUT", "content": "hi"})
    (Path.home() / ".gemini").symlink_to(gemini, target_is_directory=True)
    assert {r["source"] for r in history()} == {str(session), str(transcript)}
