---
description: Extract all Claude Code session logs for this project
allowed-tools: Bash(python3 scripts/extract_session_log.py:*)
---

Run the session log extractor and report what it wrote.

!`python3 scripts/extract_session_log.py --all $ARGUMENTS`

Defaults to `--all`: one `session_log_<session-id>.md` per session in the repo root. Pass-through args (optional): `--output DIR` for a different directory, `--strict` to only export sessions started in this exact directory (no parent-dir walk, no newest-anywhere fallback), or a single session id / `--output -` to override and extract just one. For Codex transcripts use `scripts/extract_codex_session_log.py` instead.