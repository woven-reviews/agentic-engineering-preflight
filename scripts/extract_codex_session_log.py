#!/usr/bin/env python3
"""Extract a Codex session transcript into a readable markdown conversation log.

Codex records CLI sessions as JSONL files under:

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Those logs have a different schema from Claude Code transcripts. Each line is
an event envelope with a top-level ``type`` such as ``session_meta``,
``event_msg``, or ``response_item``. The useful conversation data lives under
``payload``:

* ``event_msg`` / ``payload.type == "user_message"`` starts a real user turn.
* ``response_item`` / ``payload.type == "message"`` and role ``assistant`` is
  assistant text.
* ``response_item`` / ``payload.type == "function_call"`` is a tool call.
* ``response_item`` / ``payload.type == "function_call_output"`` is a tool
  result.

This script is mechanical: no LLM, no summarization, and no network access.
It intentionally ignores base instructions, developer messages, reasoning
payloads, token counts, and encrypted/internal fields.

Examples
--------
Most recent transcript for the current project, written to
``codex_session_log.md`` in the project root::

    python3 scripts/extract_codex_session_log.py

Every transcript for the current project, one file per session::

    python3 scripts/extract_codex_session_log.py --all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "codex_session_log.md"

TOOL_DESC_MAX = 120
RESULT_NOTE_MAX = 160

# Tools that ask the human for a direct answer. Codex has used both bare and
# namespaced tool names in logs, so matching is suffix-based in build_turns.
INTERACTION_TOOLS = {"request_user_input"}

_NOISE_TAG_BLOCK = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)>.*?"
    r"</\1>",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_TAG_LOOSE = re.compile(
    r"</?(system-reminder|local-command-stdout|local-command-stderr|command-stdout|"
    r"command-stderr|command-name|command-message|command-args|task-notification|"
    r"bash-stdout|bash-stderr)\b[^>]*/?>",
    re.IGNORECASE,
)
_BASH_INPUT = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL | re.IGNORECASE)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def clean_user_text(text: str) -> str:
    """Strip harness-injected wrappers while keeping the human's prose."""
    if not text:
        return ""
    cleaned = _NOISE_TAG_BLOCK.sub("", text)
    cleaned = _NOISE_TAG_LOOSE.sub("", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", cleaned)
    return cleaned.strip()


def _parse_ts(ts: Optional[str]):
    if not ts or not isinstance(ts, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def format_timestamp(ts: Optional[str]) -> str:
    if not ts or not isinstance(ts, str):
        return "(no timestamp)"
    dt = _parse_ts(ts)
    if dt is None:
        return ts
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_elapsed(first_ts: Optional[str], ts: Optional[str]) -> str:
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
    if formatted == "(no timestamp)":
        return "unknown"
    return formatted.split(" ")[0]


def _jsonl_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*.jsonl") if p.is_file()]


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def load_entries(path: Path) -> List[Dict[str, Any]]:
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
        print(f"warning: skipped {bad} malformed/blank lines total", file=sys.stderr)
    return entries


def _payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else {}


def session_id_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(50), fh):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    sid = _payload(obj).get("id")
                    return sid if isinstance(sid, str) else None
    except OSError:
        return None
    return None


def session_cwd_from_file(path: Path) -> Optional[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(80), fh):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = _payload(obj)
                if obj.get("type") == "session_meta":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
                if obj.get("type") == "turn_context":
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except ValueError:
        pass
    try:
        b.resolve().relative_to(a.resolve())
        return True
    except ValueError:
        return False


def _cwd_matches(transcript_cwd: Optional[str], cwd: Path, strict: bool = False) -> bool:
    if not transcript_cwd:
        return False
    rec = Path(transcript_cwd).expanduser()
    if strict:
        return rec.resolve() == cwd.resolve()
    return _paths_overlap(rec, cwd)


def matching_transcripts(root: Path, cwd: Path, strict: bool = False) -> List[Path]:
    """Return every Codex transcript whose recorded cwd overlaps ``cwd``.

    In ``strict`` mode the recorded cwd must equal ``cwd`` exactly (no
    ancestor/descendant overlap).
    """
    return [
        p
        for p in _jsonl_files(root)
        if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]


def output_identifier(transcript: Path) -> str:
    """Stable filename identifier for one transcript.

    Prefer Codex's session id from ``session_meta``. Fall back to the rollout
    filename stem when older logs do not carry an id.
    """
    raw = session_id_from_file(transcript) or transcript.stem
    ident = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")
    return ident or transcript.stem


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    sessions_root: Optional[str],
    strict: bool = False,
) -> Path:
    """Pick a Codex transcript.

    Precedence:
      1. --transcript PATH
      2. positional selector that is an existing file
      3. positional selector as filename/session-id prefix
      4. newest transcript whose recorded cwd overlaps the current cwd
      5. newest transcript anywhere under ~/.codex/sessions

    In ``strict`` mode step 4 requires an exact cwd match and step 5 (newest
    anywhere) is disabled, so only sessions started in the current directory
    are ever selected.
    """
    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    if selector:
        p = Path(selector).expanduser()
        if p.is_file():
            return p

    root = Path(sessions_root).expanduser() if sessions_root else CODEX_SESSIONS_ROOT
    files = _jsonl_files(root)

    if selector:
        matches: List[Path] = []
        for p in files:
            sid = session_id_from_file(p)
            if p.stem.startswith(selector) or (sid and sid.startswith(selector)):
                matches.append(p)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            joined = "\n  ".join(str(p) for p in matches)
            raise FileNotFoundError(
                f"session selector {selector!r} is ambiguous; matches:\n  {joined}"
            )
        raise FileNotFoundError(f"no Codex transcript matching {selector!r}")

    cwd = Path.cwd()
    cwd_matches = [
        p for p in files if _cwd_matches(session_cwd_from_file(p), cwd, strict=strict)
    ]
    picked = newest(cwd_matches)
    if picked is not None:
        return picked

    if strict:
        raise FileNotFoundError(
            f"no Codex transcript for {cwd} under {root} "
            "(--strict: cwd-overlap and newest-anywhere fallback disabled)"
        )

    picked = newest(files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        f"could not locate any Codex transcript (looked under {root}); "
        "pass --transcript PATH"
    )


def _content_text(content: Any, text_keys: Tuple[str, ...]) -> str:
    parts: List[str] = []
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in text_keys:
                    val = block.get(key)
                    if isinstance(val, str):
                        parts.append(val)
                        break
    return "\n".join(parts)


def user_text_from_event(payload: Dict[str, Any]) -> str:
    text = payload.get("message")
    if isinstance(text, str):
        return clean_user_text(text)
    return ""


def assistant_text_from_payload(payload: Dict[str, Any]) -> str:
    return _content_text(payload.get("content"), ("text", "output_text")).strip()


def _decode_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    return arguments


def tool_descriptor(name: str, arguments: Any) -> str:
    args = _decode_arguments(arguments)
    if not isinstance(args, dict):
        return _truncate(str(args), TOOL_DESC_MAX) if args else ""

    for key in ("cmd", "command", "query", "pattern", "path", "file_path", "url"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return _truncate(val, TOOL_DESC_MAX)

    # multi_tool_use.parallel stores nested recipient calls.
    tool_uses = args.get("tool_uses")
    if isinstance(tool_uses, list):
        names = []
        for item in tool_uses:
            if isinstance(item, dict) and isinstance(item.get("recipient_name"), str):
                names.append(item["recipient_name"])
        if names:
            return _truncate(", ".join(names), TOOL_DESC_MAX)

    try:
        return _truncate(json.dumps(args, ensure_ascii=False), TOOL_DESC_MAX)
    except (TypeError, ValueError):
        return ""


def result_note(output: Any) -> str:
    if isinstance(output, str):
        return _truncate(output, RESULT_NOTE_MAX)
    try:
        return _truncate(json.dumps(output, ensure_ascii=False), RESULT_NOTE_MAX)
    except (TypeError, ValueError):
        return ""


def _is_interaction_tool(name: str) -> bool:
    return any(name == tool or name.endswith("." + tool) for tool in INTERACTION_TOOLS)


def _decode_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _value_to_answer_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_value_to_answer_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in (
            "answer",
            "answer_text",
            "value",
            "label",
            "selected_label",
            "selected",
            "selection",
            "choice",
            "choices",
            "labels",
        ):
            if key in value:
                text = _value_to_answer_text(value[key])
                if text:
                    return text
    return ""


def _extract_answer_from_item(question: Dict[str, Any], item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    qid = question.get("id")
    qtext = question.get("question")
    header = question.get("header")
    item_ids = (
        item.get("id"),
        item.get("question_id"),
        item.get("questionId"),
        item.get("question"),
        item.get("header"),
    )
    if any(marker and marker in item_ids for marker in (qid, qtext, header)):
        return _value_to_answer_text(item)
    return ""


def _structured_answer_for_question(question: Dict[str, Any], output: Any) -> str:
    output = _decode_jsonish(output)
    qid = question.get("id")
    qtext = question.get("question")
    header = question.get("header")

    if isinstance(output, dict):
        for key in (qid, qtext, header):
            if isinstance(key, str) and key in output:
                text = _value_to_answer_text(output[key])
                if text:
                    return text

        for container_key in ("answers", "responses", "selections", "values"):
            container = output.get(container_key)
            if isinstance(container, dict):
                for key in (qid, qtext, header):
                    if isinstance(key, str) and key in container:
                        text = _value_to_answer_text(container[key])
                        if text:
                            return text
                for value in container.values():
                    text = _extract_answer_from_item(question, value)
                    if text:
                        return text
            elif isinstance(container, list):
                for item in container:
                    text = _extract_answer_from_item(question, item)
                    if text:
                        return text

        text = _extract_answer_from_item(question, output)
        if text:
            return text

    if isinstance(output, list):
        for item in output:
            text = _extract_answer_from_item(question, item)
            if text:
                return text

    return ""


def _flatten_result_text(output: Any) -> str:
    output = _decode_jsonish(output)
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return str(output)
    if isinstance(output, list):
        return " ".join(_flatten_result_text(item) for item in output)
    if isinstance(output, dict):
        parts: List[str] = []
        for key in ("message", "text", "output", "content", "answer", "value"):
            if key in output:
                text = _flatten_result_text(output[key])
                if text:
                    parts.append(text)
        if not parts:
            for value in output.values():
                text = _flatten_result_text(value)
                if text:
                    parts.append(text)
        return " ".join(parts)
    return ""


def _parse_chosen(
    question_text: str, options: List[Dict[str, Any]], result_text: str
) -> Tuple[str, List[Dict[str, Any]]]:
    val = ""
    if question_text and result_text:
        marker = f'"{question_text}"="'
        idx = result_text.find(marker)
        if idx != -1:
            rest = result_text[idx + len(marker) :]
            end = rest.find('"')
            val = rest[:end] if end != -1 else rest
    if not val and result_text:
        parts = result_text.split('="', 1)
        if len(parts) == 2:
            tail = parts[1]
            end = tail.find('"')
            val = tail[:end] if end != -1 else tail
    chosen = [o for o in options if o.get("label") and o["label"] in val]
    return val, chosen


def option_qa_from_output(
    output: Any, questions: Optional[List[Any]]
) -> List[Dict[str, Any]]:
    if not questions:
        return []

    result_text = _flatten_result_text(output)
    out: List[Dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = [o for o in q.get("options", []) if isinstance(o, dict)]
        val, chosen = _parse_chosen(q.get("question", ""), options, result_text)
        if not val:
            val = _structured_answer_for_question(q, output)
            chosen = [o for o in options if o.get("label") and o["label"] in val]
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


def patch_paths(arguments: Any) -> List[str]:
    args = _decode_arguments(arguments)
    if isinstance(args, dict):
        text = args.get("patch") or args.get("input") or args.get("content")
    else:
        text = args
    if not isinstance(text, str):
        return []

    paths: List[str] = []
    for line in text.splitlines():
        m = re.match(r"\*\*\* (?:Add|Update|Delete) File: (.+)", line)
        if m:
            paths.append(m.group(1).strip())
    return paths


def append_unique_path(paths: List[str], seen: set, path: str) -> None:
    """Append a changed path, avoiding relative/absolute duplicates."""
    if not path:
        return
    for existing in seen:
        if existing == path or existing.endswith("/" + path) or path.endswith("/" + existing):
            return
    seen.add(path)
    paths.append(path)


class Turn:
    def __init__(self, user_text: str, timestamp: Optional[str]):
        self.user_text = user_text
        self.timestamp = timestamp
        self.shell_command: Optional[str] = None
        self.assistant_text_blocks: List[str] = []
        self.tool_bullets: List[str] = []
        self.result_notes: List[str] = []
        self.option_qas: List[Dict[str, Any]] = []

    def add_assistant_text(self, text: str) -> None:
        if text and text.strip():
            self.assistant_text_blocks.append(text.strip())

    def add_tool(self, name: str, descriptor: str) -> None:
        if descriptor:
            self.tool_bullets.append(f"- {name} -> {descriptor}")
        else:
            self.tool_bullets.append(f"- {name}")

    def add_result_note(self, note: str) -> None:
        if note:
            self.result_notes.append(note)


def build_turns(entries: List[Dict[str, Any]]) -> Tuple[List[Turn], List[str]]:
    turns: List[Turn] = []
    files_changed: List[str] = []
    files_seen = set()
    current: Optional[Turn] = None
    call_names: Dict[str, str] = {}
    pending_questions: Dict[str, List[Any]] = {}

    for entry in entries:
        etype = entry.get("type")
        payload = _payload(entry)
        ptype = payload.get("type")

        if etype == "event_msg":
            if ptype == "user_message":
                text = user_text_from_event(payload)
                if not text:
                    continue
                current = Turn(text, entry.get("timestamp"))
                cmds = [c.strip() for c in _BASH_INPUT.findall(text) if c.strip()]
                if cmds:
                    current.shell_command = "\n".join(cmds)
                turns.append(current)
                continue

            if ptype == "patch_apply_end":
                changes = payload.get("changes")
                if isinstance(changes, dict):
                    for fp in changes:
                        if isinstance(fp, str):
                            append_unique_path(files_changed, files_seen, fp)
                if current is not None:
                    stdout = payload.get("stdout")
                    stderr = payload.get("stderr")
                    note = result_note(stdout or stderr)
                    if note:
                        current.add_result_note("apply_patch: " + note)
                continue

        if etype != "response_item":
            continue

        if ptype == "message":
            role = payload.get("role")
            if role == "assistant":
                if current is None:
                    current = Turn("", entry.get("timestamp"))
                    turns.append(current)
                current.add_assistant_text(assistant_text_from_payload(payload))
            # Ignore developer/system/user context response_items. Real user turns
            # are represented by event_msg:user_message above.
            continue

        if ptype in ("function_call", "custom_tool_call"):
            if current is None:
                current = Turn("", entry.get("timestamp"))
                turns.append(current)
            name = payload.get("name") if isinstance(payload.get("name"), str) else "tool"
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                call_names[call_id] = name
            arguments = payload.get("arguments")
            if arguments is None:
                arguments = payload.get("input")
            current.add_tool(name, tool_descriptor(name, arguments))
            args = _decode_arguments(arguments)

            if isinstance(call_id, str) and _is_interaction_tool(name):
                qs = args.get("questions") if isinstance(args, dict) else None
                if isinstance(qs, list):
                    pending_questions[call_id] = qs

            if name.endswith("apply_patch") or name == "apply_patch":
                for fp in patch_paths(arguments):
                    append_unique_path(files_changed, files_seen, fp)
            continue

        if ptype in ("function_call_output", "custom_tool_call_output"):
            if current is None:
                continue
            call_id = payload.get("call_id")
            prefix = ""
            if isinstance(call_id, str) and call_id in call_names:
                prefix = f"{call_names[call_id]}: "
            output = payload.get("output")
            questions = (
                pending_questions.get(call_id) if isinstance(call_id, str) else None
            )
            qas = option_qa_from_output(output, questions)
            if qas:
                current.option_qas.extend(qas)
                continue
            note = result_note(output)
            if note:
                current.add_result_note(prefix + note)
            continue

    return turns, files_changed


def first_timestamp(entries: List[Dict[str, Any]]) -> Optional[str]:
    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def derive_project_label(transcript: Path, entries: List[Dict[str, Any]]) -> str:
    for entry in entries:
        payload = _payload(entry)
        if entry.get("type") in ("session_meta", "turn_context"):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                sid = payload.get("id")
                if isinstance(sid, str) and sid:
                    return f"{cwd}  (session: {sid})"
                return cwd
    return str(transcript)


def _derive_context_line(turns: List[Turn]) -> str:
    for turn in turns:
        if turn.user_text.strip():
            first = _truncate(turn.user_text, 80)
            return f"a Codex session starting with: \"{first}\""
    return "a Codex session"


def render_summary(turns: List[Turn], first_ts: Optional[str]) -> List[str]:
    """Render the up-front recap of every real user input."""
    out: List[str] = ["## Summary - user inputs", ""]
    input_turns = [(i, t) for i, t in enumerate(turns, 1) if t.user_text.strip()]
    if not input_turns:
        out += ["_(No user inputs in this transcript.)_", ""]
        return out

    prev_ts: Optional[str] = None
    for i, turn in input_turns:
        elapsed = format_elapsed(first_ts, turn.timestamp) or "+?"
        delta = format_elapsed(prev_ts, turn.timestamp) if prev_ts else "+0s"
        out.append(
            f"### Turn {i} - {format_timestamp(turn.timestamp)} "
            f"({elapsed}, delta {delta})"
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
            for option in qa["options"]:
                mark = "x" if option.get("label") in qa["chosen_labels"] else " "
                row = f"- [{mark}] **{option.get('label', '')}**"
                if option.get("description"):
                    row += f" - {option['description']}"
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
    out.append("# Codex Session Conversation Log")
    out.append("")
    out.append(
        f"A turn-by-turn log of the conversation for {_derive_context_line(turns)}."
    )
    out.append(f"Project: {project_label}. Date: {date_only(first_ts)}.")
    out.append("")
    out.append("---")

    if not turns:
        out.append("")
        out.append("_(No conversational turns found in this Codex transcript.)_")
        out.append("")
        return "\n".join(out)

    out.append("")
    out.extend(render_summary(turns, first_ts))
    out.append("---")
    out.append("")
    out.append("# Full turn-by-turn detail")

    for i, turn in enumerate(turns, 1):
        out.append("")
        elapsed = format_elapsed(first_ts, turn.timestamp)
        header = f"## Turn {i} - {format_timestamp(turn.timestamp)}"
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
            out.append("**User:** _(no user text - pre-conversation activity)_")
        out.append("")

        if turn.assistant_text_blocks:
            assistant_body = "\n\n".join(turn.assistant_text_blocks)
            out.append(f"**Assistant:** {assistant_body}")
        elif turn.tool_bullets:
            out.append("**Assistant:**")
        else:
            out.append("**Assistant:** _(no response captured)_")

        if turn.tool_bullets:
            out.append("")
            out.extend(turn.tool_bullets)

        if turn.result_notes:
            notes = "; ".join(turn.result_notes[:6])
            extra = len(turn.result_notes) - 6
            if extra > 0:
                notes += f"; (+{extra} more results)"
            out.append("")
            out.append(f"  _results:_ {_truncate(notes, 500)}")

        if turn.option_qas:
            out.append("")
            for qa in turn.option_qas:
                chosen = (
                    ", ".join(c for c in qa["chosen_labels"] if c)
                    or qa["answer_text"]
                    or "(no selection)"
                )
                q = qa["header"] or _truncate(qa["question"], 80)
                out.append(f"  _user chose:_ {q} -> {chosen}")

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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_codex_session_log.py",
        description=(
            "Extract a Codex CLI session JSONL transcript into a markdown "
            "conversation log. Mechanical extraction only; no LLM or network."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Selection precedence:\n"
            "  --transcript PATH > positional file path > positional session id/prefix\n"
            "  > newest transcript whose recorded cwd overlaps the current cwd\n"
            "  > newest transcript anywhere under ~/.codex/sessions."
        ),
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="session id/prefix OR path to a Codex .jsonl transcript",
    )
    parser.add_argument(
        "--transcript",
        metavar="PATH",
        help="explicit path to a .jsonl transcript (overrides positional selection)",
    )
    parser.add_argument(
        "--sessions-root",
        metavar="PATH",
        help="override Codex sessions root (default: ~/.codex/sessions)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only read sessions started in the current directory: require an "
            "exact recorded-cwd match (no overlap) and disable the "
            "newest-anywhere global fallback"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every Codex session whose recorded cwd overlaps the current "
            "working directory, one file per session named "
            "codex_session_log_<session-id>.md (in this mode --output is treated "
            "as the output directory; positional selector is ignored)"
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output markdown file (default: codex_session_log.md in the project "
            f"root, {DEFAULT_OUTPUT}; use '-' for stdout). With --all, an output "
            "directory instead (default: project root)."
        ),
    )
    return parser


def render_transcript(transcript: Path) -> str:
    """Parse one Codex transcript file and return its rendered markdown."""
    entries = load_entries(transcript)
    if not entries:
        raise ValueError(f"no parseable JSON entries found in {transcript}")
    turns, files_changed = build_turns(entries)
    return render(
        turns=turns,
        files_changed=files_changed,
        project_label=derive_project_label(transcript, entries),
        first_ts=first_timestamp(entries),
    )


def _unique_identifier(base: str, used: set) -> str:
    ident = base
    suffix = 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


def _extract_all(
    sessions_root: Optional[str], output: Optional[str], strict: bool = False
) -> int:
    """Extract every matching Codex transcript, one markdown file each."""
    root = Path(sessions_root).expanduser() if sessions_root else CODEX_SESSIONS_ROOT
    cwd = Path.cwd()
    files = sorted(
        matching_transcripts(root, cwd, strict=strict),
        key=lambda p: (p.stat().st_mtime, str(p)),
    )
    if not files:
        print(
            f"error: no Codex transcripts for {cwd} under {root}",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} Codex session(s) from {root}", file=sys.stderr)

    written = 0
    used_ids = set()
    for transcript in files:
        ident = _unique_identifier(output_identifier(transcript), used_ids)
        try:
            markdown = render_transcript(transcript)
        except (OSError, ValueError) as exc:
            print(f"  skip {ident}: {exc}", file=sys.stderr)
            continue

        out_path = out_dir / f"codex_session_log_{ident}.md"
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
        out = None if args.output == "-" else args.output
        return _extract_all(args.sessions_root, out, strict=args.strict)

    try:
        transcript = select_transcript(
            selector=args.session,
            transcript=args.transcript,
            sessions_root=args.sessions_root,
            strict=args.strict,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"using transcript: {transcript}", file=sys.stderr)

    try:
        markdown = render_transcript(transcript)
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
