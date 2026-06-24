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
