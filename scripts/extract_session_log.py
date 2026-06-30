#!/usr/bin/env python3
"""Extract a Claude Code session transcript into a readable markdown conversation log.

Claude Code records each working session as a JSONL file (one JSON object per
line) under ``~/.claude/projects/<encoded-project-path>/<session-id>.jsonl``.
The project path is "encoded" by replacing every run of non-alphanumeric
characters with a single dash, e.g.::

    /Users/jane/work/app  ->  -Users-jane-work-app

This script reads one of those transcripts and produces a turn-by-turn markdown
log with per-turn timestamps. It is a *mechanical* extractor: there is no LLM,
no summarization, and no network access. User prose is reproduced verbatim;
assistant text is reproduced verbatim; tool calls are condensed to a single
bullet each (tool name + a short descriptor such as the file path or command).

A "turn" is a real user message together with everything the assistant did in
response, up to the next real user message. Tool results come back as
``user``-role entries whose content is ``tool_result`` blocks -- those are *not*
real user messages and do not start a new turn; they are folded (condensed) into
the preceding assistant activity. Subagent / sidechain entries
(``isSidechain == true``) and meta entries (``isMeta == true``) are kept out of
the main flow.

Standard library only; works on Python 3.8+.

Examples
--------
Most recent transcript for the current project, written to ``session_log.md``
in the project root (always, regardless of where you run it from)::

    python3 scripts/extract_session_log.py

A specific session id, to stdout::

    python3 scripts/extract_session_log.py 1a2b3c4d-... --output -

An explicit transcript file::

    python3 scripts/extract_session_log.py --transcript ~/.claude/projects/-Users-me-app/abc.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# This script lives at ``<project_root>/scripts/extract_session_log.py``, so the
# project root is two levels up. The log is always written here by default,
# regardless of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "session_log.md"

# Tool calls whose inputs touch files we want to collect for "Files changed".
FILE_WRITING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Create"}

# Tools that prompt the human for a direct answer (pop-up questions, etc.). Their
# tool_result carries the human's selection, so we surface it as a user
# interaction rather than folding it into the generic result notes.
INTERACTION_TOOLS = {"AskUserQuestion"}

# Keys that, across tool versions, hold a target file path.
FILE_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")

# Max length for a condensed tool descriptor before it gets an ellipsis.
TOOL_DESC_MAX = 100
# Max length for a condensed tool-result note.
RESULT_NOTE_MAX = 120

# Patterns for harness noise embedded in user-message text. These wrappers are
# injected by the CLI, not typed by the human, so we strip them while keeping
# the surrounding human prose.
_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
# Self-closing / unmatched variants and bare opening tags of the same family.
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)

# A `!`-prefixed shell command the human ran in-session is recorded as a user
# message wrapped in <bash-input>…</bash-input>. We surface it as a shell command
# rather than as prose. (The matching <bash-stdout>/<bash-stderr> output arrives
# as a separate user message and is stripped as noise above.)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Transcript discovery
# --------------------------------------------------------------------------- #


def encode_project_path(path: Path) -> str:
    """Encode a filesystem path the way Claude Code names its project dirs.

    Every run of characters that is not ``[A-Za-z0-9]`` collapses to a single
    dash. This mirrors the observed encoding (``/`` and ``.`` both become ``-``).
    """
    raw = str(path)
    return re.sub(r"[^A-Za-z0-9]+", "-", raw)


def _candidate_project_dirs(cwd: Path, strict: bool = False) -> Iterable[Path]:
    """Yield plausible encoded-project dirs for ``cwd``, most specific first.

    Tries the cwd itself and then each parent, so running from a subdirectory of
    the project still resolves to the project's transcript dir. In ``strict``
    mode only the cwd itself is considered -- no parent-walk.
    """
    seen = set()
    bases = [cwd] if strict else [cwd, *cwd.parents]
    for base in bases:
        encoded = encode_project_path(base)
        d = PROJECTS_ROOT / encoded
        if d not in seen:
            seen.add(d)
            yield d


def resolve_project_dir(
    explicit: Optional[str], cwd: Path, strict: bool = False
) -> Optional[Path]:
    """Resolve the encoded project dir.

    If ``explicit`` is given it wins (and need not exist yet -- caller validates).
    Otherwise look for an existing transcript dir matching the cwd or one of its
    parents. Returns ``None`` if nothing matched (caller falls back globally).
    In ``strict`` mode only the exact cwd is matched (no parent-walk).
    """
    if explicit:
        return Path(explicit).expanduser()
    for d in _candidate_project_dirs(cwd, strict=strict):
        if d.is_dir():
            return d
    return None


def _jsonl_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.is_file() and p.suffix == ".jsonl"]


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    project_dir: Optional[str],
    strict: bool = False,
) -> Path:
    """Pick the transcript file to parse, following the documented precedence.

    Precedence:
      1. ``--transcript PATH`` (explicit file).
      2. positional ``selector`` that is itself a path to an existing file.
      3. positional ``selector`` treated as a session id, looked up in the
         resolved project dir (with or without the ``.jsonl`` suffix).
      4. newest ``.jsonl`` in the resolved project dir.
      5. newest ``.jsonl`` across every project dir (global fallback).

    In ``strict`` mode the project dir must match the cwd exactly (no
    parent-walk) and the global fallback (step 5) is disabled, so the script
    only ever reads sessions started in the current directory.

    Raises ``FileNotFoundError`` with a clear message when nothing resolves.
    """
    cwd = Path.cwd()

    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    # A positional selector that points directly at a file.
    if selector:
        as_path = Path(selector).expanduser()
        if as_path.is_file():
            return as_path

    proj = resolve_project_dir(project_dir, cwd, strict=strict)

    # A positional selector treated as a session id within the project dir.
    if selector and proj is not None:
        for name in (selector, f"{selector}.jsonl"):
            candidate = proj / name
            if candidate.is_file():
                return candidate
        # Allow a prefix match on the session id (ids are long UUIDs).
        prefix_matches = [p for p in _jsonl_files(proj) if p.stem.startswith(selector)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            joined = "\n  ".join(str(p) for p in prefix_matches)
            raise FileNotFoundError(
                f"session id {selector!r} is ambiguous; matches:\n  {joined}"
            )

    # Newest in the resolved project dir.
    if proj is not None:
        picked = newest(_jsonl_files(proj))
        if picked is not None:
            return picked

    # If a selector was given but never resolved, that is an error -- do not
    # silently fall through to an unrelated transcript.
    if selector:
        where = f" in {proj}" if proj is not None else ""
        raise FileNotFoundError(f"no transcript matching {selector!r}{where}")

    # Strict mode: never reach past the cwd's own project dir.
    if strict:
        raise FileNotFoundError(
            f"no Claude Code transcript for {cwd} under {PROJECTS_ROOT} "
            "(--strict: parent-walk and global fallback disabled)"
        )

    # Global fallback: newest .jsonl across all project dirs.
    all_files: List[Path] = []
    if PROJECTS_ROOT.is_dir():
        for d in PROJECTS_ROOT.iterdir():
            all_files.extend(_jsonl_files(d))
    picked = newest(all_files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        "could not locate any Claude Code transcript "
        f"(looked under {PROJECTS_ROOT}); pass --transcript PATH"
    )


# --------------------------------------------------------------------------- #
# JSONL loading
# --------------------------------------------------------------------------- #


def load_entries(path: Path) -> List[Dict[str, Any]]:
    """Load a JSONL transcript, skipping blank/malformed lines.

    A single bad line never aborts the run; it is reported to stderr (rate
    limited) and skipped.
    """
    entries: List[Dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                if bad <= 5:
                    print(
                        f"warning: skipping malformed JSON on line {lineno}",
                        file=sys.stderr,
                    )
                continue
            if isinstance(obj, dict):
                entries.append(obj)
            else:
                bad += 1
    if bad > 5:
        print(
            f"warning: skipped {bad} malformed/blank lines total",
            file=sys.stderr,
        )
    return entries


# --------------------------------------------------------------------------- #
# Content-block helpers
# --------------------------------------------------------------------------- #


def _get_message(entry: Dict[str, Any]) -> Dict[str, Any]:
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else {}


def _content_blocks(entry: Dict[str, Any]) -> List[Any]:
    """Return the message content as a list of blocks.

    Content may be a plain string (older / simple user messages) or a list of
    typed blocks. Always normalize to a list so callers can iterate.
    """
    content = _get_message(entry).get("content")
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _is_tool_result_entry(entry: Dict[str, Any]) -> bool:
    """True if this user-role entry is a tool result, not a human message."""
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return True
    # Some versions stash the result under a top-level toolUseResult and leave
    # content empty; treat those as tool results too.
    if entry.get("toolUseResult") is not None and not _human_text(entry).strip():
        return True
    return False


def _human_text(entry: Dict[str, Any]) -> str:
    """Concatenate the text blocks of a (user or assistant) message."""
    parts: List[str] = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    # Collapse the blank-line runs that removal can leave behind.
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())  # flatten whitespace/newlines for one-liners
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"  # ellipsis


def tool_descriptor(name: str, tool_input: Any) -> str:
    """Build a short one-line descriptor for a tool_use block."""
    if not isinstance(tool_input, dict):
        return _truncate(str(tool_input), TOOL_DESC_MAX) if tool_input else ""

    # File-oriented tools: show the path.
    for key in FILE_PATH_KEYS:
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

    # Shell.
    if "command" in tool_input and isinstance(tool_input["command"], str):
        return _truncate(tool_input["command"], TOOL_DESC_MAX)

    # Common search/agent tools.
    for key in ("pattern", "query", "url", "prompt", "description"):
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

    # Fallback: compact JSON of the input.
    try:
        return _truncate(json.dumps(tool_input, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def extract_file_path(tool_input: Any) -> Optional[str]:
    if not isinstance(tool_input, dict):
        return None
    for key in FILE_PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def result_note(entry: Dict[str, Any]) -> str:
    """A terse note about a tool result (error flag / short output)."""
    is_error = False
    text_bits: List[str] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error"):
            is_error = True
        content = block.get("content")
        if isinstance(content, str):
            text_bits.append(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    text_bits.append(c["text"])

    if not text_bits:
        tur = entry.get("toolUseResult")
        if isinstance(tur, str):
            text_bits.append(tur)
        elif isinstance(tur, dict):
            for k in ("stdout", "stderr", "output", "content"):
                v = tur.get(k)
                if isinstance(v, str) and v.strip():
                    text_bits.append(v)
                    break

    blob = _truncate(" ".join(text_bits), RESULT_NOTE_MAX)
    if is_error:
        return f"error: {blob}" if blob else "error"
    return blob


def _result_block_text(block: Dict[str, Any]) -> str:
    """Flatten a tool_result block's content to a string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c["text"]
            for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        )
    return ""


def _parse_chosen(
    question_text: str, options: List[Dict[str, Any]], result_text: str
) -> Tuple[str, List[Dict[str, Any]]]:
    """Pull the chosen answer for one question out of the result string.

    The AskUserQuestion result is self-describing, e.g.
    ``Your questions have been answered: "<question>"="<label>", "<q2>"="<l2>".``
    For multiSelect the value is comma-joined labels; for an "Other" answer the
    value is free text matching no option. We locate the value by anchoring on
    the exact question text, then mark every option whose label appears in it.
    """
    val = ""
    if question_text and result_text:
        marker = f'"{question_text}"="'
        idx = result_text.find(marker)
        if idx != -1:
            rest = result_text[idx + len(marker) :]
            end = rest.find('"')
            val = rest[:end] if end != -1 else rest
    if not val and result_text:
        # Single-question fallback: first value after an '="'.
        parts = result_text.split('="', 1)
        if len(parts) == 2:
            tail = parts[1]
            end = tail.find('"')
            val = tail[:end] if end != -1 else tail
    chosen = [o for o in options if o.get("label") and o["label"] in val]
    return val, chosen


def option_qa_from_result(
    entry: Dict[str, Any], pending_questions: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Build structured Q/options/choice records for an AskUserQuestion result.

    Returns [] for any tool_result that isn't a tracked pop-up question.
    """
    out: List[Dict[str, Any]] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        qid = block.get("tool_use_id")
        questions = pending_questions.get(qid) if isinstance(qid, str) else None
        if not questions:
            continue
        result_text = _result_block_text(block)
        for q in questions:
            if not isinstance(q, dict):
                continue
            options = [o for o in q.get("options", []) if isinstance(o, dict)]
            val, chosen = _parse_chosen(q.get("question", ""), options, result_text)
            out.append(
                {
                    "question": q.get("question", ""),
                    "header": q.get("header", ""),
                    "options": options,
                    "chosen_labels": [o.get("label") for o in chosen],
                    "answer_text": val,
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Timestamp formatting
# --------------------------------------------------------------------------- #


def _parse_ts(ts: Optional[str]):
    """Parse an ISO-8601 timestamp to a datetime, or None if unparseable."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        # Python's fromisoformat dislikes a trailing 'Z' before 3.11.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def format_timestamp(ts: Optional[str]) -> str:
    """Render an ISO-8601 timestamp as 'YYYY-MM-DD HH:MM:SS' in local time.

    Falls back to the raw string, then to '(no timestamp)'.
    """
    if not ts or not isinstance(ts, str):
        return "(no timestamp)"
    dt = _parse_ts(ts)
    if dt is None:
        return ts
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # to local time
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_elapsed(first_ts: Optional[str], ts: Optional[str]) -> str:
    """Time since session start, as '+H:MM:SS' / '+M:SS' / '+Ns'. '' if unknown."""
    a, b = _parse_ts(first_ts), _parse_ts(ts)
    if a is None or b is None:
        return ""
    secs = max(0, int((b - a).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"+{h}:{m:02d}:{s:02d}"
    if m:
        return f"+{m}:{s:02d}"
    return f"+{s}s"


def date_only(ts: Optional[str]) -> str:
    formatted = format_timestamp(ts)
    if formatted in ("(no timestamp)",):
        return "unknown"
    return formatted.split(" ")[0]


# --------------------------------------------------------------------------- #
# Turn assembly
# --------------------------------------------------------------------------- #


class Turn:
    """One human message and the assistant activity that followed it."""

    def __init__(self, user_text: str, timestamp: Optional[str]):
        self.user_text = user_text
        self.timestamp = timestamp
        # Set when the user's input was a `!`-prefixed shell command.
        self.shell_command: Optional[str] = None
        self.assistant_text_blocks: List[str] = []
        self.tool_bullets: List[str] = []  # already-rendered "- Name — desc"
        self.result_notes: List[str] = []
        self.option_qas: List[
            Dict[str, Any]
        ] = []  # AskUserQuestion: Q + options + choice
        self.subagents: List[Dict[str, Any]] = []  # folded subagent summaries
        self.subagent_count = 0

    def add_assistant_text(self, text: str) -> None:
        if text and text.strip():
            self.assistant_text_blocks.append(text.strip())

    def add_tool(self, name: str, descriptor: str) -> None:
        if descriptor:
            self.tool_bullets.append(f"- {name} — {descriptor}")
        else:
            self.tool_bullets.append(f"- {name}")

    def add_result_note(self, note: str) -> None:
        if note:
            self.result_notes.append(note)


def build_turns(entries: List[Dict[str, Any]]) -> Tuple[List[Turn], List[str]]:
    """Walk entries in order, producing turns and the changed-files list."""
    turns: List[Turn] = []
    files_changed: List[str] = []
    files_seen = set()
    current: Optional[Turn] = None
    pending_subagents = 0  # sidechain entries seen since the last real user msg
    pending_questions: Dict[str, Any] = {}  # tool_use id -> AskUserQuestion questions

    for entry in entries:
        etype = entry.get("type")

        # Meta and summary entries never participate in the main flow.
        if entry.get("isMeta") is True:
            continue
        if etype in ("summary", "system"):
            continue

        # Sidechain (subagent) entries are kept out of the flow; we only count
        # the user-prompt that *spawns* a subagent so we can note it.
        if entry.get("isSidechain") is True:
            if etype == "user" and not _is_tool_result_entry(entry):
                pending_subagents += 1
            continue

        if etype == "user":
            if _is_tool_result_entry(entry):
                # Fold the result into the current turn, condensed.
                if current is not None:
                    qas = option_qa_from_result(entry, pending_questions)
                    if qas:
                        current.option_qas.extend(qas)
                    else:
                        note = result_note(entry)
                        if note:
                            current.add_result_note(note)
                continue
            # A real human message: start a new turn.
            text = clean_user_text(_human_text(entry))
            # An entry that becomes empty after stripping harness noise is not a
            # meaningful turn on its own; skip it.
            if not text:
                continue
            current = Turn(text, entry.get("timestamp"))
            cmds = [c.strip() for c in _BASH_INPUT.findall(text) if c.strip()]
            if cmds:
                current.shell_command = "\n".join(cmds)
            if pending_subagents:
                current.subagent_count += pending_subagents
                pending_subagents = 0
            turns.append(current)

        elif etype == "assistant":
            if current is None:
                # Assistant activity before any human turn (rare); make a stub.
                current = Turn("", entry.get("timestamp"))
                turns.append(current)
            for block in _content_blocks(entry):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    current.add_assistant_text(block.get("text", ""))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    tool_input = block.get("input")
                    current.add_tool(name, tool_descriptor(name, tool_input))
                    if name in INTERACTION_TOOLS and isinstance(block.get("id"), str):
                        qs = (
                            tool_input.get("questions")
                            if isinstance(tool_input, dict)
                            else None
                        )
                        if isinstance(qs, list):
                            pending_questions[block["id"]] = qs
                    if name in FILE_WRITING_TOOLS:
                        fp = extract_file_path(tool_input)
                        if fp and fp not in files_seen:
                            files_seen.add(fp)
                            files_changed.append(fp)
        # Unknown types are ignored silently (robust to schema additions).

    # If subagents were spawned at the very end with no following user turn,
    # attribute them to the last real turn.
    if pending_subagents and turns:
        turns[-1].subagent_count += pending_subagents

    return turns, files_changed


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _derive_context_line(turns: List[Turn]) -> str:
    """A brief context line from the first human message, if usable."""
    for t in turns:
        if t.user_text.strip():
            first = _truncate(t.user_text, 80)
            return f"a Claude Code working session starting with: “{first}”"
    return "a Claude Code working session"


def render_summary(turns: List[Turn], first_ts: Optional[str]) -> List[str]:
    """The up-front recap: every turn that carried real user input.

    Turns with no user text (pre-conversation / assistant-only activity) are
    omitted — they required no input. User prose is reproduced in full. When the
    input was a pop-up choice, all presented options are listed with the chosen
    one(s) checked.
    """
    out: List[str] = ["## Summary — user inputs", ""]
    input_turns = [(i, t) for i, t in enumerate(turns, 1) if t.user_text.strip()]
    if not input_turns:
        out += ["_(No user inputs in this transcript.)_", ""]
        return out

    prev_ts: Optional[str] = None
    for i, turn in input_turns:
        elapsed = format_elapsed(first_ts, turn.timestamp) or "+?"
        delta = format_elapsed(prev_ts, turn.timestamp) if prev_ts else "+0s"
        out.append(
            f"### Turn {i} · {format_timestamp(turn.timestamp)} ({elapsed}, Δ{delta})"
        )
        out.append("")
        if turn.shell_command:
            out.append("Ran shell command:")
            out.append("")
            out.append("```sh")
            out.append(turn.shell_command)
            out.append("```")
        else:
            for line in turn.user_text.splitlines():
                out.append(f"> {line}" if line.strip() else ">")
        out.append("")
        for qa in turn.option_qas:
            label = qa["header"] or "Question"
            out.append(f"_Answered via options ({label}):_")
            if qa["question"]:
                out.append(f"> {qa['question']}")
            for o in qa["options"]:
                mark = "x" if o.get("label") in qa["chosen_labels"] else " "
                row = f"- [{mark}] **{o.get('label', '')}**"
                if o.get("description"):
                    row += f" — {o['description']}"
                out.append(row)
            if not qa["chosen_labels"] and qa["answer_text"]:
                out.append(f"- [x] _(custom answer)_ {qa['answer_text']}")
            out.append("")
        prev_ts = turn.timestamp
    return out


def render(
    turns: List[Turn],
    files_changed: List[str],
    project_label: str,
    first_ts: Optional[str],
) -> str:
    out: List[str] = []
    out.append("# Session Conversation Log")
    out.append("")
    out.append(
        f"A turn-by-turn log of the conversation for {_derive_context_line(turns)}."
    )
    out.append(f"Project: {project_label}. Date: {date_only(first_ts)}.")
    out.append("")
    out.append("---")

    if not turns:
        out.append("")
        out.append("_(No conversational turns found in this transcript.)_")
        out.append("")
        return "\n".join(out)

    # Summary first, then the full detail.
    out.append("")
    out.extend(render_summary(turns, first_ts))
    out.append("---")
    out.append("")
    out.append("# Full turn-by-turn detail")

    for i, turn in enumerate(turns, 1):
        out.append("")
        elapsed = format_elapsed(first_ts, turn.timestamp)
        header = f"## Turn {i} · {format_timestamp(turn.timestamp)}"
        if elapsed:
            header += f" ({elapsed} into session)"
        out.append(header)
        out.append("")
        if turn.shell_command:
            out.append("**User ran shell command:**")
            out.append("")
            out.append("```sh")
            out.append(turn.shell_command)
            out.append("```")
        elif turn.user_text.strip():
            out.append(f"**User:** {turn.user_text}")
        else:
            out.append("**User:** _(no user text — pre-conversation activity)_")
        out.append("")

        assistant_chunks: List[str] = []
        if turn.assistant_text_blocks:
            assistant_chunks.append("\n\n".join(turn.assistant_text_blocks))
        body = "\n\n".join(c for c in assistant_chunks if c)
        if body:
            out.append(f"**Assistant:** {body}")
        elif turn.tool_bullets:
            out.append("**Assistant:**")
        else:
            out.append("**Assistant:** _(no response captured)_")

        if turn.tool_bullets:
            out.append("")
            out.extend(turn.tool_bullets)

        if turn.result_notes:
            # Keep results compact: one summarizing line.
            notes = "; ".join(turn.result_notes[:6])
            extra = len(turn.result_notes) - 6
            if extra > 0:
                notes += f"; (+{extra} more results)"
            out.append("")
            out.append(f"  _results:_ {_truncate(notes, 400)}")

        if turn.option_qas:
            out.append("")
            for qa in turn.option_qas:
                chosen = (
                    ", ".join(c for c in qa["chosen_labels"] if c)
                    or qa["answer_text"]
                    or "(no selection)"
                )
                q = qa["header"] or _truncate(qa["question"], 80)
                out.append(f"  _user chose:_ {q} → {chosen}")

        for s in turn.subagents:
            out.append("")
            label = s["agent_type"]
            if s["task"]:
                out.append(f"  _subagent ({label}):_ {_truncate(s['task'], 160)}")
            else:
                out.append(f"  _subagent ({label})_")
            detail = f"    → {s['tool_count']} tool call(s)"
            if s["tool_names"]:
                detail += f" ({', '.join(s['tool_names'])})"
            if s["result"]:
                detail += f"; result: {_truncate(s['result'], 200)}"
            out.append(detail)

        if turn.subagent_count:
            out.append("")
            out.append(f"_(spawned {turn.subagent_count} subagent(s))_")

        out.append("")
        out.append("---")

    if files_changed:
        out.append("")
        out.append("## Files changed during the session")
        out.append("")
        out.append("```")
        out.extend(files_changed)
        out.append("```")

    out.append("")
    return "\n".join(out)


def derive_project_label(
    transcript: Path, entries: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Best-effort human-readable project label.

    Prefer the real project path: transcript entries carry a ``cwd`` field, which
    is the accurate working directory. Fall back to de-dashing the encoded dir
    name (lossy — the encoding collapses '/' and '_' to '-', so the original
    separators can't be recovered).
    """
    encoded = transcript.parent.name
    # Accurate path from the transcript itself, when available.
    if entries:
        for entry in entries:
            cwd = entry.get("cwd")
            if isinstance(cwd, str) and cwd:
                return f"{cwd}  (dir: {encoded})" if encoded else cwd
    # Encoded dirs typically begin with a leading dash (from the leading '/').
    decoded = "/" + encoded.lstrip("-").replace("-", "/") if encoded else encoded
    if encoded:
        return f"{decoded}  (dir: {encoded})"
    return str(transcript.parent)


def first_timestamp(entries: List[Dict[str, Any]]) -> Optional[str]:
    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


# --------------------------------------------------------------------------- #
# Subagents (current format: a sibling <session-id>/subagents/agent-*.jsonl dir)
# --------------------------------------------------------------------------- #


def summarize_subagent(path: Path) -> Optional[Dict[str, Any]]:
    """Condense one subagent transcript into task / tools / result.

    There is no reliable link from the main transcript to a subagent file (no
    Task tool_use id, no back-reference), so we attribute by time elsewhere. The
    only sidecar metadata is ``agentType`` in ``agent-<id>.meta.json``.
    """
    entries = load_entries(path)
    if not entries:
        return None

    agent_type = "subagent"
    meta_path = path.parent / (path.stem + ".meta.json")
    if meta_path.is_file():
        try:
            agent_type = json.loads(meta_path.read_text("utf-8")).get(
                "agentType", agent_type
            )
        except (OSError, ValueError):
            pass

    # The spawning prompt is the first real (non-tool-result) user message.
    task = ""
    for e in entries:
        if e.get("type") == "user" and not _is_tool_result_entry(e):
            t = clean_user_text(_human_text(e))
            if t:
                task = t
                break

    # Tool calls and the final assistant text, in one pass.
    tools: List[str] = []
    result = ""
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for b in _content_blocks(e):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tools.append(b.get("name", "tool"))
            elif b.get("type") == "text":
                txt = b.get("text")
                if isinstance(txt, str) and txt.strip():
                    result = txt.strip()  # keep the last one

    return {
        "agent_type": agent_type,
        "start_ts": first_timestamp(entries),
        "task": task,
        "tool_count": len(tools),
        "tool_names": sorted(set(tools)),
        "result": result,
    }


def load_subagents(transcript: Path) -> List[Dict[str, Any]]:
    """Summarize every subagent transcript for this session, oldest first."""
    d = transcript.parent / transcript.stem / "subagents"
    subs: List[Dict[str, Any]] = []
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix == ".jsonl":
                s = summarize_subagent(p)
                if s:
                    subs.append(s)
    subs.sort(key=lambda s: s.get("start_ts") or "")
    return subs


def attribute_subagents(turns: List[Turn], subs: List[Dict[str, Any]]) -> None:
    """Fold each subagent under the latest turn that started at or before it."""
    parsed = [(_parse_ts(t.timestamp), t) for t in turns]
    for s in subs:
        st = _parse_ts(s.get("start_ts"))
        target: Optional[Turn] = None
        if st is not None:
            for ts, t in parsed:  # turns are in order; last match wins
                if ts is not None and ts <= st:
                    target = t
        if target is None and turns:
            target = turns[0]
        if target is not None:
            target.subagents.append(s)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="extract_session_log.py",
        description=(
            "Extract a Claude Code session transcript (JSONL) into a markdown "
            "conversation log with per-turn timestamps. Mechanical extraction "
            "only — no LLM, no summarization."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Selection precedence:\n"
            "  --transcript PATH  >  positional file path  >  positional session id\n"
            "  >  newest .jsonl in the resolved project dir  >  newest .jsonl anywhere.\n\n"
            "The project dir is resolved from the current working directory by\n"
            "encoding its path (non-alphanumerics -> '-') under ~/.claude/projects/."
        ),
    )
    p.add_argument(
        "session",
        nargs="?",
        help="session id (full or unique prefix) OR a path to a .jsonl transcript",
    )
    p.add_argument(
        "--transcript",
        metavar="PATH",
        help="explicit path to a .jsonl transcript (overrides positional selection)",
    )
    p.add_argument(
        "--project-dir",
        metavar="PATH",
        help=(
            "override the encoded project dir under ~/.claude/projects/ "
            "(default: resolved from the current working directory)"
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only read sessions started in the current directory: disable the "
            "parent-dir walk and the newest-anywhere global fallback"
        ),
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every session in the resolved project dir, one file per "
            "session named session_log_<session-id>.md (in this mode --output is "
            "treated as the output directory; positional selector is ignored)"
        ),
    )
    p.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output markdown file (default: session_log.md in the project root, "
            f"{DEFAULT_OUTPUT}; use '-' for stdout). With --all, an output "
            "directory instead (default: project root)."
        ),
    )
    return p


def render_transcript(transcript: Path, project_label_override: Optional[str]) -> str:
    """Parse one transcript file and return its rendered markdown.

    Raises ``OSError`` if the file can't be read and ``ValueError`` if it holds
    no parseable entries.
    """
    entries = load_entries(transcript)
    if not entries:
        raise ValueError(f"no parseable JSON entries found in {transcript}")
    turns, files_changed = build_turns(entries)
    subagents = load_subagents(transcript)
    attribute_subagents(turns, subagents)
    project_label = project_label_override or derive_project_label(transcript, entries)
    return render(
        turns=turns,
        files_changed=files_changed,
        project_label=project_label,
        first_ts=first_timestamp(entries),
    )


def _extract_all(
    project_dir: Optional[Path], output: Optional[str], strict: bool = False
) -> int:
    """Extract every transcript in the project dir, one markdown file each.

    The session id (the ``.jsonl`` stem) is the per-file identifier. Files are
    written to ``output`` (treated as a directory) or the project root.
    """
    cwd = Path.cwd()
    proj = project_dir or resolve_project_dir(None, cwd, strict=strict)
    if proj is None or not proj.is_dir():
        where = proj if proj is not None else f"(none matched cwd under {PROJECTS_ROOT})"
        print(f"error: no project transcript dir found: {where}", file=sys.stderr)
        return 1

    files = sorted(_jsonl_files(proj), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"error: no .jsonl transcripts in {proj}", file=sys.stderr)
        return 1

    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} session(s) from {proj}", file=sys.stderr)

    written = 0
    for transcript in files:
        session_id = transcript.stem
        try:
            markdown = render_transcript(transcript, None)
        except (OSError, ValueError) as exc:
            print(f"  skip {session_id}: {exc}", file=sys.stderr)
            continue
        out_path = out_dir / f"session_log_{session_id}.md"
        try:
            out_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            print(f"  error writing {out_path}: {exc}", file=sys.stderr)
            continue
        written += 1
        print(f"  wrote {out_path}", file=sys.stderr)

    print(f"done: {written}/{len(files)} session(s) written", file=sys.stderr)
    return 0 if written else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.all:
        proj = Path(args.project_dir).expanduser() if args.project_dir else None
        out = None if args.output == "-" else args.output
        return _extract_all(proj, out, strict=args.strict)

    try:
        transcript = select_transcript(
            selector=args.session,
            transcript=args.transcript,
            project_dir=args.project_dir,
            strict=args.strict,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"using transcript: {transcript}", file=sys.stderr)

    try:
        markdown = render_transcript(
            transcript,
            args.project_dir if args.project_dir else None,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.output == "-":
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")
    else:
        out_path = (
            DEFAULT_OUTPUT if args.output is None else Path(args.output).expanduser()
        )
        try:
            out_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
