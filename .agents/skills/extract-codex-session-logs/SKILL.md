---
name: extract-codex-session-logs
description: Manual-only helper for exporting all Codex session logs for the python_rfp_manager repo by running scripts/extract_codex_session_log.py with --all. Use only when explicitly invoked as $extract-codex-session-logs or selected from the skills UI.
---

# Extract Codex Session Logs

Run the Codex session-log extractor for every Codex transcript associated with the `python_rfp_manager` app repo.

1. Set the command working directory to the `python_rfp_manager` repo root, the directory containing `scripts/extract_codex_session_log.py`.
2. Run:

```bash
python3 scripts/extract_codex_session_log.py --all
```

3. If the user supplied extra arguments after invoking the skill, append them after `--all`. Common supported arguments are `--output DIR`, `--sessions-root DIR`, and `--strict` (only export sessions started in this exact directory — no parent/descendant cwd overlap, no newest-anywhere fallback).
4. Report the files written from the command output.

Do not edit the generated `codex_session_log_*.md` files unless the user explicitly asks.
