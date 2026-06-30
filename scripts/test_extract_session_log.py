#!/usr/bin/env python3
"""Minimal asserts for the option-question parsing in extract_session_log.

Run: python3 scripts/test_extract_session_log.py
"""

from __future__ import annotations

from pathlib import Path

from extract_session_log import (
    _candidate_project_dirs,
    _parse_chosen,
    clean_user_text,
)

OPTS = [
    {"label": "section", "description": "a"},
    {"label": "manufacturer_part_no", "description": "b"},
    {"label": "Neither", "description": "c"},
]


def test_single_choice():
    res = 'Your questions have been answered: "Q"="section". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert val == "section", val
    assert [o["label"] for o in chosen] == ["section"]


def test_multiselect_comma():
    res = '"Q"="section, manufacturer_part_no". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert [o["label"] for o in chosen] == ["section", "manufacturer_part_no"], chosen


def test_custom_answer_matches_no_option():
    res = '"Q"="something the user typed". continue.'
    val, chosen = _parse_chosen("Q", OPTS, res)
    assert val == "something the user typed"
    assert chosen == []


def test_multi_question_anchors_on_its_own_text():
    res = '"Q1"="section", "Q2"="Neither". continue.'
    _, c1 = _parse_chosen("Q1", OPTS, res)
    _, c2 = _parse_chosen("Q2", OPTS, res)
    assert [o["label"] for o in c1] == ["section"], c1
    assert [o["label"] for o in c2] == ["Neither"], c2


def test_task_notification_is_stripped_to_empty():
    assert clean_user_text("<task-notification>\nstuff\n</task-notification>") == ""


def test_bash_output_is_stripped_to_empty():
    msg = "<bash-stdout>some output</bash-stdout><bash-stderr>a warning</bash-stderr>"
    assert clean_user_text(msg) == ""


def test_bash_input_survives_cleaning_for_detection():
    # The command itself must NOT be stripped; build_turns extracts it.
    assert (
        clean_user_text("<bash-input>ls -la</bash-input>")
        == "<bash-input>ls -la</bash-input>"
    )


def test_strict_candidate_dirs_skip_parents():
    cwd = Path("/Users/me/work/app/sub")
    loose = list(_candidate_project_dirs(cwd, strict=False))
    strict = list(_candidate_project_dirs(cwd, strict=True))
    assert len(strict) == 1  # only the cwd itself
    assert len(loose) > 1  # cwd + parents
    assert strict[0] == loose[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
