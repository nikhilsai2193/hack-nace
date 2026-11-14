# Study Room — developer notes for coding agents

This file is for agents working ON this repository. It must never reach a study room's Intern session: each room's
workspace lives outside the repo (`$STUDYROOM_DATA_DIR/<room>/workspace`, default `/tmp/studyroom`), because Intern
reads AGENTS.md from the working directory upward to the nearest `.git`.

- `server/` FastAPI backend. `intern_runner.py` speaks Intern's JSON-RPC over stdio; `normalizer.py` maps raw Intern
  events to UI events (display only); `grounding.py` handles sources, the manifest and the app-side citation check.
- `server/intern_config/` templates copied per room: `SYSTEM2.md`, `SYSTEM1.md`, `config.toml` into INTERN_HOME,
  `AGENTS.md` into the workspace. System 1 policy goes ONLY in INTERN_HOME's SYSTEM1.md.
- `web/` Next.js frontend; it talks only to `NEXT_PUBLIC_API_BASE_URL`.

Hard rules: no mock or offline System 1/System 2 path, no fake partner responses, no app-side judging or blocking of
tool calls (that is Drex's job inside Intern), never send API keys to the browser.

Checks: `cd web && npm run typecheck && npm run build`; `cd server && .venv/bin/python -m pytest -q tests`.
