# Environment Preflight

A tiny app on the **same stack** you'll use for the assessment — FastAPI + Postgres backend,
React + TypeScript + Vite frontend, all orchestrated with Docker Compose. It does nothing
interesting on purpose: its only job is to confirm your machine can **build and run the stack**
*before* your timer starts, so you don't lose any of your window to setup.

Run this ahead of time. If it comes up green, you're ready.

## Run it

```bash
docker compose watch     # or: docker compose up --build
```

First run pulls base images and installs dependencies — that's the slow part, and running it
now means it's cached when the real app arrives. Subsequent runs are fast.

Then open:

- **Frontend:** <http://localhost:5173> — should show **✅ Stack is up** with `database: ok`.
- **Backend docs:** <http://localhost:8000/docs>
- **API health:** <http://localhost:8000/api/v1/utils/health-check/> → `true`

Seeing the green check on the frontend means the whole chain works: the frontend built and
served, reached the backend, and the backend reached Postgres.

## If it doesn't come up

- **Docker not running** — start Docker Desktop and retry.
- **Port already in use** (5173, 8000, 5432, 8080, 80) — stop whatever's using it, or change the
  host port in `compose.override.yml`.
- **Slow first build** — expected; let it finish once.

Tear down with `docker compose down` (add `-v` to also drop the database volume).

## Exporting session logs

`scripts/` has two mechanical extractors that turn an AI coding session into a readable
markdown log (no LLM, no network — stdlib only). Run them from the repo root:

```bash
python3 scripts/extract_session_log.py          # Claude Code sessions (~/.claude/projects/)
python3 scripts/extract_codex_session_log.py    # Codex CLI sessions (~/.codex/sessions/)
```

By default each writes the most recent session for the current directory to
`session_log.md` / `codex_session_log.md` in the repo root.

Useful flags (both scripts):

- `--all` — one file per session (`session_log_<id>.md` / `codex_session_log_<id>.md`).
- `--strict` — only sessions started in *this exact directory*; skips the parent-dir walk
  and the "newest session anywhere" fallback, so you never accidentally export an unrelated
  project's transcript.
- `--output DIR` — write to `DIR` instead of the repo root (or `--output -` for stdout).
- a session id (or unique prefix) — export just that one session.

### From inside your AI harness

Each tool ships a shortcut so you can export without leaving the session:

- **Claude Code** — run the `/extract-session-log` slash command
  (`.claude/commands/extract-session-log.md`). Defaults to `--all`; append any of the flags
  above, e.g. `/extract-session-log --strict`.
- **Codex** — invoke the `extract-codex-session-logs` skill (as `$extract-codex-session-logs`
  or from the skills UI; see `.agents/skills/extract-codex-session-logs/`). It runs the Codex
  extractor with `--all`; extra args like `--strict` or `--output DIR` are passed through.

