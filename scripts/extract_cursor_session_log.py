#!/usr/bin/env python3
"""Extract a Cursor Agent session transcript into a readable markdown log.

Cursor Agent records sessions as JSONL under::

    ~/.cursor/projects/<encoded-workspace-path>/agent-transcripts/
        <session-id>/<session-id>.jsonl          # nested (current)
        <session-id>.jsonl                       # flat (legacy)
        <session-id>/subagents/<id>.jsonl        # delegated subagents

The workspace path is encoded by stripping a leading ``/`` and replacing every
``/`` with ``-``, e.g.::

    /Volumes/Data/dev/app  ->  Volumes-Data-dev-app

Each line is either a chat message::

    {"role": "user"|"assistant", "message": {"content": [ ... ]}}

or a turn boundary marker::

    {"type": "turn_ended", "status": "success"|"error", ...}

Content blocks are Anthropic-style ``text`` / ``tool_use`` (and occasionally
``image``). Tool results are *not* recorded in these transcripts. Pasted images
usually arrive as an ``<image_files>`` block listing paths under the project's
``assets/`` directory.

This script is mechanical: no LLM, no summarization, no network. Stdlib only;
Python 3.8+.

Examples
--------
Most recent transcript for the current workspace::

    python3 scripts/extract_cursor_session_log.py

Every transcript for the current workspace, one file per session::

    python3 scripts/extract_cursor_session_log.py --all

A specific session id, to stdout::

    python3 scripts/extract_cursor_session_log.py 1a2b3c4d-... --output -
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECTS_ROOT = Path.home() / ".cursor" / "projects"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "cursor_session_log.md"
IMAGE_DUMP_DIR = Path(tempfile.gettempdir()) / "cursor_session_images"

_IMAGE_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

FILE_WRITING_TOOLS = {
    "Write",
    "StrReplace",
    "Delete",
    "EditNotebook",
    "ApplyPatch",
    "Create",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
}
FILE_PATH_KEYS = ("path", "file_path", "notebook_path", "filePath", "target_notebook")

TOOL_DESC_MAX = 100

_TIMESTAMP_TAG = re.compile(
    r"<timestamp>\s*(.*?)\s*</timestamp>", re.DOTALL | re.IGNORECASE
)
_USER_QUERY_TAG = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE
)
_IMAGE_FILES_TAG = re.compile(
    r"<image_files>\s*(.*?)\s*</image_files>", re.DOTALL | re.IGNORECASE
)
_IMAGE_PATH_LINE = re.compile(
    r"^\s*\d+\.\s+(\S.+?)\s*$", re.MULTILINE
)
_LEADING_IMAGE_MARK = re.compile(r"^\[Image\]\s*", re.IGNORECASE)
# Injected after background/shell waits; not typed by the human.
_META_USER_QUERY = re.compile(
    r"^Briefly inform the user about the task result\b",
    re.IGNORECASE,
)
_DATA_IMAGE_URL = re.compile(
    r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL
)
# "Saturday, Oct 3, 2026, 11:00 AM (UTC+2)"
_HUMAN_TS = re.compile(
    r"^([A-Za-z]+),\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4}),\s+"
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Transcript discovery
# --------------------------------------------------------------------------- #


def encode_project_path(path: Path) -> str:
    """Encode a filesystem path the way Cursor names project dirs under
    ``~/.cursor/projects/``.

    Observed rule: strip a leading ``/``, then replace every ``/`` with ``-``.
    Other characters (including ``-`` and ``.``) are kept as-is.
    """
    raw = str(path)
    if raw.startswith("/"):
        raw = raw[1:]
    return raw.replace("/", "-")


def _candidate_transcript_dirs(cwd: Path, strict: bool = False) -> Iterable[Path]:
    """Yield ``.../agent-transcripts`` dirs for ``cwd``, most specific first.

    Tries both the given path and its ``resolve()``d form so macOS
    ``/var`` ↔ ``/private/var`` (and similar symlink roots) still match the
    directory name Cursor recorded when the workspace was opened.
    """
    seen = set()
    roots: List[Path] = []
    for candidate in (cwd, cwd.resolve()):
        if candidate not in roots:
            roots.append(candidate)
    bases: List[Path] = []
    for root in roots:
        for base in [root] if strict else [root, *root.parents]:
            if base not in bases:
                bases.append(base)
    for base in bases:
        d = PROJECTS_ROOT / encode_project_path(base) / "agent-transcripts"
        if d not in seen:
            seen.add(d)
            yield d


def resolve_transcript_dir(
    explicit: Optional[str], cwd: Path, strict: bool = False
) -> Optional[Path]:
    """Resolve the agent-transcripts directory for ``cwd``.

    ``explicit`` may be either the encoded project dir under
    ``~/.cursor/projects/`` or the ``agent-transcripts`` dir itself.
    """
    if explicit:
        p = Path(explicit).expanduser()
        if p.name == "agent-transcripts":
            return p
        nested = p / "agent-transcripts"
        if nested.is_dir() or not p.is_dir():
            return nested
        return p
    for d in _candidate_transcript_dirs(cwd, strict=strict):
        if d.is_dir():
            return d
    return None


def _jsonl_transcripts(directory: Path) -> List[Path]:
    """Main-session transcripts only (skip ``subagents/``)."""
    if not directory.is_dir():
        return []
    out: List[Path] = []
    for child in directory.iterdir():
        if child.is_file() and child.suffix == ".jsonl":
            out.append(child)
            continue
        if child.is_dir() and child.name != "subagents":
            main = child / f"{child.name}.jsonl"
            if main.is_file():
                out.append(main)
    return out


def newest(paths: Iterable[Path]) -> Optional[Path]:
    paths = list(paths)
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def session_id_from_path(transcript: Path) -> str:
    """Stable session id: directory name for nested layout, else stem."""
    if transcript.parent.name == transcript.stem:
        return transcript.stem
    return transcript.stem


def select_transcript(
    selector: Optional[str],
    transcript: Optional[str],
    project_dir: Optional[str],
    strict: bool = False,
) -> Path:
    """Pick a Cursor transcript.

    Precedence:
      1. --transcript PATH
      2. positional selector that is an existing file
      3. positional selector as session id / prefix in the resolved project
      4. newest transcript in the resolved project
      5. newest transcript anywhere under ~/.cursor/projects
    """
    cwd = Path.cwd()

    if transcript:
        p = Path(transcript).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"--transcript path does not exist: {p}")
        return p

    if selector:
        as_path = Path(selector).expanduser()
        if as_path.is_file():
            return as_path

    proj = resolve_transcript_dir(project_dir, cwd, strict=strict)

    if selector and proj is not None:
        files = _jsonl_transcripts(proj)
        exact = [
            p
            for p in files
            if session_id_from_path(p) == selector
            or p.name == selector
            or p.name == f"{selector}.jsonl"
        ]
        if len(exact) == 1:
            return exact[0]
        prefix_matches = [
            p for p in files if session_id_from_path(p).startswith(selector)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        if len(prefix_matches) > 1:
            joined = "\n  ".join(str(p) for p in prefix_matches)
            raise FileNotFoundError(
                f"session id {selector!r} is ambiguous; matches:\n  {joined}"
            )

    if proj is not None:
        picked = newest(_jsonl_transcripts(proj))
        if picked is not None:
            return picked

    if selector:
        where = f" in {proj}" if proj is not None else ""
        raise FileNotFoundError(f"no Cursor transcript matching {selector!r}{where}")

    if strict:
        raise FileNotFoundError(
            f"no Cursor Agent transcript for {cwd} under {PROJECTS_ROOT} "
            "(--strict: parent-walk and global fallback disabled)"
        )

    all_files: List[Path] = []
    if PROJECTS_ROOT.is_dir():
        for d in PROJECTS_ROOT.iterdir():
            if d.is_dir():
                all_files.extend(_jsonl_transcripts(d / "agent-transcripts"))
    picked = newest(all_files)
    if picked is not None:
        return picked

    raise FileNotFoundError(
        "could not locate any Cursor Agent transcript "
        f"(looked under {PROJECTS_ROOT}); pass --transcript PATH"
    )


# --------------------------------------------------------------------------- #
# JSONL loading
# --------------------------------------------------------------------------- #


def load_entries(path: Path) -> List[Dict[str, Any]]:
    """Load a JSONL transcript, skipping blank/malformed lines."""
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
# Content helpers
# --------------------------------------------------------------------------- #


def _get_message(entry: Dict[str, Any]) -> Dict[str, Any]:
    msg = entry.get("message")
    return msg if isinstance(msg, dict) else {}


def _content_blocks(entry: Dict[str, Any]) -> List[Any]:
    content = _get_message(entry).get("content")
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _human_text(entry: Dict[str, Any]) -> str:
    parts: List[str] = []
    for block in _content_blocks(entry):
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text")
            if isinstance(txt, str):
                parts.append(txt)
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def extract_image_file_paths(text: str) -> List[str]:
    """Paths listed inside Cursor's ``<image_files>`` harness block."""
    paths: List[str] = []
    for match in _IMAGE_FILES_TAG.finditer(text or ""):
        for line_match in _IMAGE_PATH_LINE.finditer(match.group(1)):
            path = line_match.group(1).strip()
            if path:
                paths.append(path)
    return paths


def clean_user_text(text: str) -> Tuple[Optional[str], str, List[str]]:
    """Return ``(timestamp, cleaned_prose, image_paths)`` from a user message."""
    if not text:
        return None, "", []

    timestamp: Optional[str] = None
    m = _TIMESTAMP_TAG.search(text)
    if m:
        timestamp = m.group(1).strip() or None
        text = text[: m.start()] + text[m.end() :]

    image_paths = extract_image_file_paths(text)
    text = _IMAGE_FILES_TAG.sub("", text)
    text = _LEADING_IMAGE_MARK.sub("", text)

    queries = [q.strip() for q in _USER_QUERY_TAG.findall(text) if q.strip()]
    if queries:
        text = "\n\n".join(queries)
    else:
        text = re.sub(r"</?user_query\b[^>]*>", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text).strip()
    return timestamp, text, image_paths


def is_meta_user_text(text: str) -> bool:
    return bool(text and _META_USER_QUERY.match(text.strip()))


def tool_descriptor(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return _truncate(str(tool_input), TOOL_DESC_MAX) if tool_input else ""

    if name == "CallDynamicTool":
        ns = tool_input.get("namespace")
        tn = tool_input.get("toolName")
        if isinstance(ns, str) and isinstance(tn, str):
            args = tool_input.get("arguments")
            if isinstance(args, dict) and args:
                return _truncate(f"{ns}.{tn} {json.dumps(args, ensure_ascii=False)}", TOOL_DESC_MAX)
            return f"{ns}.{tn}"

    for key in FILE_PATH_KEYS:
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

    if isinstance(tool_input.get("command"), str):
        return _truncate(tool_input["command"], TOOL_DESC_MAX)

    for key in ("pattern", "query", "searchTerm", "url", "prompt", "description", "glob_pattern"):
        if key in tool_input and isinstance(tool_input[key], str):
            return _truncate(tool_input[key], TOOL_DESC_MAX)

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


def _image_sources_from_entry(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect image refs from content blocks (base64 / data URL / path)."""
    out: List[Dict[str, Any]] = []
    for block in _content_blocks(entry):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "image":
            src = block.get("source")
            if isinstance(src, dict):
                out.append(src)
            path = block.get("path") or block.get("url")
            if isinstance(path, str) and path:
                out.append({"type": "path", "path": path})
        elif btype in ("image_url", "input_image"):
            url = block.get("image_url") or block.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                out.append({"type": "data_url", "data_url": url})
            elif isinstance(url, str):
                out.append({"type": "path", "path": url})
    return out


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    m = _HUMAN_TS.match(ts.strip())
    if not m:
        return None
    try:
        return datetime.strptime(
            f"{m.group(2)} {m.group(3)} {m.group(4)} {m.group(5)}:{m.group(6)} {m.group(7)}",
            "%b %d %Y %I:%M %p",
        )
    except ValueError:
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
    # Human timestamps that failed structured parse still start with a weekday.
    if " " in formatted and formatted[0:4].isdigit():
        return formatted.split(" ")[0]
    dt = _parse_ts(ts)
    if dt is not None:
        return dt.strftime("%Y-%m-%d")
    return "unknown"


# --------------------------------------------------------------------------- #
# Turn assembly
# --------------------------------------------------------------------------- #


class Turn:
    """One human message and the assistant activity that followed it."""

    def __init__(self, user_text: str, timestamp: Optional[str]):
        self.user_text = user_text
        self.timestamp = timestamp
        self.image_paths: List[str] = []
        self.image_refs: List[Dict[str, Any]] = []
        self.assistant_text_blocks: List[str] = []
        self.tool_bullets: List[str] = []
        self.subagents: List[Dict[str, Any]] = []

    def add_assistant_text(self, text: str) -> None:
        if text and text.strip():
            self.assistant_text_blocks.append(text.strip())

    def add_tool(self, name: str, descriptor: str) -> None:
        if descriptor:
            self.tool_bullets.append(f"- **{name}** — {descriptor}")
        else:
            self.tool_bullets.append(f"- **{name}**")


def build_turns(entries: List[Dict[str, Any]]) -> Tuple[List[Turn], List[str]]:
    """Group Cursor transcript entries into user/assistant turns."""
    turns: List[Turn] = []
    current: Optional[Turn] = None
    files_changed: List[str] = []
    seen_files = set()

    def ensure_turn(timestamp: Optional[str] = None) -> Turn:
        nonlocal current
        if current is None:
            # Prefer folding orphan assistant activity into the previous turn
            # (e.g. after a skipped meta user_query / turn_ended boundary).
            if turns:
                current = turns[-1]
            else:
                current = Turn("", timestamp)
                turns.append(current)
        return current

    for entry in entries:
        etype = entry.get("type")
        if etype == "turn_ended":
            # Soft boundary; next *real* user message starts a fresh turn.
            current = None
            continue

        role = entry.get("role")
        if role == "user":
            raw = _human_text(entry)
            timestamp, prose, image_paths = clean_user_text(raw)
            image_refs = _image_sources_from_entry(entry)
            if is_meta_user_text(prose) and not image_paths and not image_refs:
                continue
            if not prose.strip() and not image_paths and not image_refs:
                continue
            current = Turn(prose, timestamp)
            current.image_paths.extend(image_paths)
            current.image_refs.extend(image_refs)
            turns.append(current)
            continue

        if role != "assistant":
            continue

        turn = ensure_turn()
        for block in _content_blocks(entry):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    turn.add_assistant_text(txt)
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                tool_input = block.get("input")
                turn.add_tool(name, tool_descriptor(name, tool_input))
                if name in FILE_WRITING_TOOLS:
                    path = extract_file_path(tool_input)
                    if path and path not in seen_files:
                        seen_files.add(path)
                        files_changed.append(path)
            elif btype in ("image", "image_url", "input_image"):
                turn.image_refs.extend(
                    _image_sources_from_entry({"message": {"content": [block]}})
                )

    return turns, files_changed


def first_timestamp(turns: List[Turn], entries: List[Dict[str, Any]]) -> Optional[str]:
    for turn in turns:
        if turn.timestamp:
            return turn.timestamp
    for entry in entries:
        raw = _human_text(entry) if entry.get("role") == "user" else ""
        ts, _, _ = clean_user_text(raw)
        if ts:
            return ts
    return None


def derive_project_label(transcript: Path, cwd_hint: Optional[Path] = None) -> str:
    """Best-effort project label; encoding is lossy for hyphens in path segments."""
    # .../projects/<encoded>/agent-transcripts/<session>/<session>.jsonl
    parts = transcript.resolve().parts
    encoded = ""
    if "agent-transcripts" in parts:
        idx = parts.index("agent-transcripts")
        if idx > 0:
            encoded = parts[idx - 1]
    if cwd_hint is not None:
        return f"{cwd_hint}  (dir: {encoded})" if encoded else str(cwd_hint)
    if encoded:
        return f"/{encoded.replace('-', '/')}  (dir: {encoded})"
    return str(transcript.parent)


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #


def _image_bytes_and_ext(ref: Dict[str, Any]) -> Optional[Tuple[bytes, str]]:
    kind = ref.get("type")

    if kind == "data_url" or (isinstance(ref.get("data_url"), str)):
        data_url = ref.get("data_url")
        if not isinstance(data_url, str):
            return None
        match = _DATA_IMAGE_URL.match(data_url)
        if not match:
            return None
        media_type, data = match.groups()
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        return raw, _IMAGE_EXT.get(media_type, "img")

    if kind == "base64" or isinstance(ref.get("data"), str) and ref.get("media_type"):
        data = ref.get("data")
        if not isinstance(data, str):
            return None
        try:
            raw = base64.b64decode(data, validate=False)
        except (ValueError, TypeError):
            return None
        media_type = ref.get("media_type")
        return raw, _IMAGE_EXT.get(media_type, "img") if isinstance(media_type, str) else "img"

    if kind == "path" or isinstance(ref.get("path"), str):
        path_value = ref.get("path")
        if not isinstance(path_value, str):
            return None
        path = Path(path_value).expanduser()
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        ext = path.suffix.lstrip(".") or "img"
        return raw, ext

    return None


def dump_images(
    turns: List[Turn], session_id: str, dump_dir: Path = IMAGE_DUMP_DIR
) -> List[Path]:
    """Copy/decode each turn's images and append a pending-description marker."""
    written: List[Path] = []
    for ti, turn in enumerate(turns, 1):
        n = 0
        for path_str in turn.image_paths:
            decoded = _image_bytes_and_ext({"type": "path", "path": path_str})
            if decoded is None:
                turn.user_text = (
                    f"{turn.user_text}\n\n"
                    f"[Image referenced as `{path_str}` — source file unavailable; "
                    f"description pending]"
                ).strip()
                continue
            n += 1
            raw, ext = decoded
            dump_dir.mkdir(parents=True, exist_ok=True)
            path = dump_dir / f"{session_id}_turn{ti}_img{n}.{ext}"
            path.write_bytes(raw)
            written.append(path)
            turn.user_text = (
                f"{turn.user_text}\n\n"
                f"[Image dumped to `{path}` — description pending]"
            ).strip()

        for ref in turn.image_refs:
            decoded = _image_bytes_and_ext(ref)
            if decoded is None:
                continue
            n += 1
            raw, ext = decoded
            dump_dir.mkdir(parents=True, exist_ok=True)
            path = dump_dir / f"{session_id}_turn{ti}_img{n}.{ext}"
            path.write_bytes(raw)
            written.append(path)
            turn.user_text = (
                f"{turn.user_text}\n\n"
                f"[Image dumped to `{path}` — description pending]"
            ).strip()
    return written


# --------------------------------------------------------------------------- #
# Subagents
# --------------------------------------------------------------------------- #


def summarize_subagent(path: Path) -> Optional[Dict[str, Any]]:
    entries = load_entries(path)
    if not entries:
        return None

    task = ""
    start_ts: Optional[str] = None
    for e in entries:
        if e.get("role") != "user":
            continue
        ts, prose, _ = clean_user_text(_human_text(e))
        if is_meta_user_text(prose):
            continue
        if prose.strip():
            task = prose
            start_ts = ts
            break

    tools: List[str] = []
    result = ""
    for e in entries:
        if e.get("role") != "assistant":
            continue
        for b in _content_blocks(e):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                tools.append(b.get("name") or "tool")
            elif b.get("type") == "text":
                txt = b.get("text")
                if isinstance(txt, str) and txt.strip():
                    result = txt.strip()

    return {
        "agent_type": "subagent",
        "start_ts": start_ts,
        "task": task,
        "tool_count": len(tools),
        "tool_names": sorted(set(tools)),
        "result": result,
        "id": path.stem,
    }


def load_subagents(transcript: Path) -> List[Dict[str, Any]]:
    """Summarize sibling ``subagents/*.jsonl`` for this session, oldest first."""
    # Nested: <session>/<session>.jsonl → <session>/subagents/
    d = transcript.parent / "subagents"
    if not d.is_dir() and transcript.parent.name != transcript.stem:
        # Flat legacy: <session>.jsonl → <session>/subagents/
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
    parsed = [(_parse_ts(t.timestamp), t) for t in turns]
    for s in subs:
        st = _parse_ts(s.get("start_ts"))
        target: Optional[Turn] = None
        if st is not None:
            for ts, t in parsed:
                if ts is not None and ts <= st:
                    target = t
        if target is None and turns:
            target = turns[-1]
        if target is not None:
            target.subagents.append(s)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _derive_context_line(turns: List[Turn]) -> str:
    for turn in turns:
        if turn.user_text.strip():
            first = _truncate(turn.user_text, 80)
            return f'a Cursor Agent session starting with: "{first}"'
    return "a Cursor Agent session"


def render_summary(turns: List[Turn], first_ts: Optional[str]) -> List[str]:
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
        for line in turn.user_text.splitlines():
            out.append(f"> {line}" if line.strip() else ">")
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
    out.append("# Cursor Session Conversation Log")
    out.append("")
    out.append(
        f"A turn-by-turn log of the conversation for {_derive_context_line(turns)}."
    )
    out.append(f"Project: {project_label}. Date: {date_only(first_ts)}.")
    out.append("")
    out.append("---")

    if not turns:
        out.append("")
        out.append("_(No conversational turns found in this Cursor transcript.)_")
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
        if turn.user_text.strip():
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

        if turn.subagents:
            out.append("")
            out.append(f"  _subagents ({len(turn.subagents)}):_")
            for s in turn.subagents:
                task = _truncate(s.get("task") or "", 120)
                tools = ", ".join(s.get("tool_names") or []) or "none"
                out.append(
                    f"  - `{s.get('id', 'subagent')}` — {s.get('tool_count', 0)} tools "
                    f"({tools}); task: {task or '(none)'}"
                )

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


def render_transcript(
    transcript: Path, cwd_hint: Optional[Path] = None
) -> str:
    entries = load_entries(transcript)
    if not entries:
        raise ValueError(f"no parseable JSON entries found in {transcript}")
    turns, files_changed = build_turns(entries)
    dump_images(turns, session_id_from_path(transcript))
    attribute_subagents(turns, load_subagents(transcript))
    return render(
        turns=turns,
        files_changed=files_changed,
        project_label=derive_project_label(transcript, cwd_hint=cwd_hint),
        first_ts=first_timestamp(turns, entries),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_cursor_session_log.py",
        description=(
            "Extract a Cursor Agent session JSONL transcript into a markdown "
            "conversation log. Mechanical extraction only; no LLM or network."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Selection precedence:\n"
            "  --transcript PATH > positional file path > positional session id/prefix\n"
            "  > newest transcript in the resolved workspace agent-transcripts dir\n"
            "  > newest transcript anywhere under ~/.cursor/projects.\n\n"
            "The workspace dir is resolved from the current working directory by\n"
            "encoding its path (strip leading '/', '/' -> '-') under\n"
            "~/.cursor/projects/<encoded>/agent-transcripts/."
        ),
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="session id (full or unique prefix) OR a path to a .jsonl transcript",
    )
    parser.add_argument(
        "--transcript",
        metavar="PATH",
        help="explicit path to a .jsonl transcript (overrides positional selection)",
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help=(
            "override the Cursor project dir or its agent-transcripts/ subdir "
            "(default: resolved from the current working directory)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "only read sessions for the current directory: disable the "
            "parent-dir walk and the newest-anywhere global fallback"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "extract every session in the resolved workspace agent-transcripts "
            "dir, one file per session named cursor_session_log_<session-id>.md "
            "(in this mode --output is treated as the output directory; "
            "positional selector is ignored)"
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            "output markdown file (default: cursor_session_log.md in the project "
            f"root, {DEFAULT_OUTPUT}; use '-' for stdout). With --all, an output "
            "directory instead (default: project root)."
        ),
    )
    return parser


def _extract_all(
    project_dir: Optional[str], output: Optional[str], strict: bool = False
) -> int:
    cwd = Path.cwd()
    proj = resolve_transcript_dir(project_dir, cwd, strict=strict)
    if proj is None or not proj.is_dir():
        where = proj if proj is not None else f"(none matched cwd under {PROJECTS_ROOT})"
        print(f"error: no Cursor agent-transcripts dir found: {where}", file=sys.stderr)
        return 1

    files = sorted(_jsonl_transcripts(proj), key=lambda p: p.stat().st_mtime)
    if not files:
        print(f"error: no .jsonl transcripts in {proj}", file=sys.stderr)
        return 1

    out_dir = Path(output).expanduser() if output else PROJECT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(files)} Cursor session(s) from {proj}", file=sys.stderr)

    written = 0
    for transcript in files:
        session_id = session_id_from_path(transcript)
        try:
            markdown = render_transcript(transcript, cwd_hint=cwd)
        except (OSError, ValueError) as exc:
            print(f"  skip {session_id}: {exc}", file=sys.stderr)
            continue
        out_path = out_dir / f"cursor_session_log_{session_id}.md"
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
        return _extract_all(args.project_dir, out, strict=args.strict)

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
        markdown = render_transcript(transcript, cwd_hint=Path.cwd())
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
