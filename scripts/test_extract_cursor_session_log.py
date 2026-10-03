#!/usr/bin/env python3
"""Minimal asserts for extract_cursor_session_log.

Run: python3 scripts/test_extract_cursor_session_log.py
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path

import extract_cursor_session_log as m


# 1x1 transparent PNG.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9"
    "awAAAABJRU5ErkJggg=="
)


def _write_jsonl(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _msg(role: str, *parts):
    return {"role": role, "message": {"content": list(parts)}}


def test_encode_project_path():
    assert (
        m.encode_project_path(Path("/Volumes/Data/dev/qualified/app"))
        == "Volumes-Data-dev-qualified-app"
    )
    assert (
        m.encode_project_path(Path("/Users/me/Desktop/tari-2026"))
        == "Users-me-Desktop-tari-2026"
    )


def test_clean_user_text_strips_wrappers_and_images():
    raw = (
        "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
        "[Image]\n"
        "<image_files>\n"
        "The following images were provided by the user and saved to the workspace "
        "for future use:\n"
        "1. /tmp/assets/shot.png\n\n"
        "These images can be copied for use in other locations.\n"
        "</image_files>\n"
        "<user_query>\nhello there\n</user_query>"
    )
    ts, prose, paths = m.clean_user_text(raw)
    assert ts == "Saturday, Oct 3, 2026, 11:00 AM (UTC+2)"
    assert prose == "hello there"
    assert paths == ["/tmp/assets/shot.png"]


def test_meta_user_queries_are_skipped():
    entries = [
        _msg(
            "user",
            {
                "type": "text",
                "text": (
                    "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                    "<user_query>\nreal question\n</user_query>"
                ),
            },
        ),
        _msg("assistant", {"type": "text", "text": "answer one"}),
        {"type": "turn_ended", "status": "success"},
        _msg(
            "user",
            {
                "type": "text",
                "text": (
                    "<timestamp>Saturday, Oct 3, 2026, 11:04 AM (UTC+2)</timestamp>\n"
                    "<user_query>Briefly inform the user about the task result "
                    "and perform any follow-up actions (if needed).</user_query>"
                ),
            },
        ),
        _msg("assistant", {"type": "text", "text": "noise"}),
        {"type": "turn_ended", "status": "success"},
    ]
    turns, _ = m.build_turns(entries)
    assert len(turns) == 1
    assert turns[0].user_text == "real question"
    # Meta follow-up is dropped; its assistant reply folds into the prior turn.
    assert turns[0].assistant_text_blocks == ["answer one", "noise"]


def test_build_turns_tools_and_files_changed():
    entries = [
        _msg(
            "user",
            {
                "type": "text",
                "text": (
                    "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                    "<user_query>\nfix the bug\n</user_query>"
                ),
            },
        ),
        _msg(
            "assistant",
            {"type": "text", "text": "Looking into it."},
            {
                "type": "tool_use",
                "name": "Shell",
                "input": {"command": "pytest -q", "description": "run tests"},
            },
            {
                "type": "tool_use",
                "name": "StrReplace",
                "input": {"path": "/tmp/app/main.py", "old_string": "a", "new_string": "b"},
            },
            {
                "type": "tool_use",
                "name": "CallDynamicTool",
                "input": {
                    "namespace": "cursor",
                    "toolName": "WebSearch",
                    "arguments": {"searchTerm": "fastapi health"},
                },
            },
        ),
        {"type": "turn_ended", "status": "success"},
    ]
    turns, files = m.build_turns(entries)
    assert len(turns) == 1
    assert turns[0].user_text == "fix the bug"
    assert "Looking into it." in turns[0].assistant_text_blocks
    bullets = "\n".join(turns[0].tool_bullets)
    assert "**Shell**" in bullets and "pytest -q" in bullets
    assert "**StrReplace**" in bullets and "/tmp/app/main.py" in bullets
    assert "cursor.WebSearch" in bullets
    assert files == ["/tmp/app/main.py"]

    md = m.render(turns, files, "proj", turns[0].timestamp)
    assert md.index("## Summary - user inputs") < md.index("# Full turn-by-turn detail")
    assert "> fix the bug" in md
    assert "## Files changed during the session" in md


def test_dump_images_from_image_files_path():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        source = tmp / "shot.png"
        source.write_bytes(base64.b64decode(_PNG_B64))
        turns, _ = m.build_turns(
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                            "[Image]\n"
                            "<image_files>\n"
                            f"1. {source}\n"
                            "</image_files>\n"
                            "<user_query>\nsee this\n</user_query>"
                        ),
                    },
                )
            ]
        )
        written = m.dump_images(turns, "sess1", dump_dir=tmp / "dumped")
        assert len(written) == 1
        assert written[0].read_bytes() == source.read_bytes()
        assert "description pending" in turns[0].user_text


def test_all_extracts_only_current_workspace():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "workspace"
        other = tmp / "other"
        projects_root = tmp / "projects"
        out = tmp / "out"
        project.mkdir()
        other.mkdir()

        # Encode the resolved path: after os.chdir, Path.cwd() is resolved and
        # on macOS may differ from the unresolved tempfile path (/var vs /private/var).
        enc = m.encode_project_path(project.resolve())
        other_enc = m.encode_project_path(other.resolve())
        sid = "11111111-1111-1111-1111-111111111111"
        other_sid = "22222222-2222-2222-2222-222222222222"

        _write_jsonl(
            projects_root / enc / "agent-transcripts" / sid / f"{sid}.jsonl",
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                            "<user_query>\nfrom workspace\n</user_query>"
                        ),
                    },
                ),
                _msg("assistant", {"type": "text", "text": "ok"}),
            ],
        )
        _write_jsonl(
            projects_root
            / other_enc
            / "agent-transcripts"
            / other_sid
            / f"{other_sid}.jsonl",
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                            "<user_query>\nfrom other\n</user_query>"
                        ),
                    },
                )
            ],
        )

        old_root = m.PROJECTS_ROOT
        old_cwd = os.getcwd()
        try:
            m.PROJECTS_ROOT = projects_root
            os.chdir(project)
            rc = m.main(["--all", "--output", str(out)])
        finally:
            os.chdir(old_cwd)
            m.PROJECTS_ROOT = old_root

        assert rc == 0
        written = sorted(p.name for p in out.glob("cursor_session_log_*.md"))
        assert written == [f"cursor_session_log_{sid}.md"]
        body = (out / written[0]).read_text(encoding="utf-8")
        assert "from workspace" in body
        assert "from other" not in body


def test_strict_does_not_walk_parents():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        project = tmp / "workspace"
        child = project / "frontend"
        projects_root = tmp / "projects"
        out = tmp / "out"
        child.mkdir(parents=True)

        enc = m.encode_project_path(project.resolve())
        sid = "33333333-3333-3333-3333-333333333333"
        _write_jsonl(
            projects_root / enc / "agent-transcripts" / sid / f"{sid}.jsonl",
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                            "<user_query>\nparent session\n</user_query>"
                        ),
                    },
                )
            ],
        )

        old_root = m.PROJECTS_ROOT
        old_cwd = os.getcwd()
        try:
            m.PROJECTS_ROOT = projects_root
            os.chdir(child)
            rc = m.main(["--all", "--strict", "--output", str(out)])
        finally:
            os.chdir(old_cwd)
            m.PROJECTS_ROOT = old_root

        assert rc == 1
        assert list(out.glob("cursor_session_log_*.md")) == []


def test_subagents_are_summarized():
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        sid = "44444444-4444-4444-4444-444444444444"
        main = tmp / sid / f"{sid}.jsonl"
        sub = tmp / sid / "subagents" / "aaaa-bbbb.jsonl"
        _write_jsonl(
            main,
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:00 AM (UTC+2)</timestamp>\n"
                            "<user_query>\ndelegate please\n</user_query>"
                        ),
                    },
                ),
                _msg(
                    "assistant",
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {"description": "explore", "prompt": "go"},
                    },
                ),
            ],
        )
        _write_jsonl(
            sub,
            [
                _msg(
                    "user",
                    {
                        "type": "text",
                        "text": (
                            "<timestamp>Saturday, Oct 3, 2026, 11:01 AM (UTC+2)</timestamp>\n"
                            "<user_query>\nExplore the repo\n</user_query>"
                        ),
                    },
                ),
                _msg(
                    "assistant",
                    {"type": "text", "text": "done exploring"},
                    {
                        "type": "tool_use",
                        "name": "Glob",
                        "input": {"glob_pattern": "**/*.py"},
                    },
                ),
            ],
        )
        md = m.render_transcript(main, cwd_hint=tmp)
        assert "subagents" in md
        assert "Explore the repo" in md
        assert "Glob" in md


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
