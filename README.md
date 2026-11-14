# Study Room

A dark, Google Meet-style AI study room. One human studies with four AI student personas (Maya, Leo, Sam and Priya),
grounded in their own handwritten notes, slides, PDFs and lecture transcripts.

**DeepInfra writes the study conversation. Nace Drex Max v1.1 decides whether the conversation and its tool calls
should go ahead.** Both run inside the official [Nace Intern harness](https://github.com/nace-ai/intern): one
long-lived `intern rpc` process per room.

## Architecture

```
browser (Next.js, web/) ──HTTP + SSE──▶ FastAPI (server/) ──JSON-RPC over stdio──▶ intern rpc
                                         │                                         ├─ System 2: deepinfra/<model>
                                         │                                         ├─ System 1: drex-max-v1.1 (Nace)
                                         │                                         └─ workspace tools: index_files,
                                         │                                            decision_files, sql, read
                                         ├─ Keenable search  → EXTERNAL source file in the workspace
                                         ├─ Adobe PDF Services → recap PDF
                                         └─ Speechmatics batch → transcript text
```

Per room, under `STUDYROOM_DATA_DIR` (default `/tmp/studyroom`, which is outside the repo):

```
<room>/home/        INTERN_HOME: SYSTEM2.md (study group + output format), SYSTEM1.md (supervision policy), config.toml
<room>/workspace/   Intern cwd:  AGENTS.md (room rules), sources/, sources/manifest.md
<room>/artifacts/   Adobe PDFs
```

The workspace sits outside the repo because Intern reads `AGENTS.md` from the working directory upward to the
nearest `.git`. Inside the repo, the developer `AGENTS.md` would leak into the study prompt. `SYSTEM1.md` is read
only from `INTERN_HOME`, never from a project, so the supervision policy lives there.

`config.toml` turns on `[capabilities.workspace]`, turns off `[capabilities.memory]` so rooms can't bleed into each
other, and keeps the `base` tool pack so System 2 can read source files. Every tool call goes through Intern's guard
and Drex.

### Why one Intern session instead of four

The four students are personas voiced by **one** System 2 session. They are not separate agents. This keeps a single
shared conversation and context, a single budget, and one System 1 supervising the whole room. With four processes,
four supervisors would each see only a quarter of the discussion, and the cost would be four times higher. Drex is
the room's supervisor, not a fifth student.

### How Drex Max v1.1 supervises

All supervision happens inside Intern, along its normal paths. The app only displays the events:

| Intern event | Shown as |
|---|---|
| `decision` purpose `supervise` | "Watching turn N: advancing / re-investigating / retrying / off plan / finished / unclear". P(re-investigating + retrying) ≥ 50% gives a warning. |
| `decision` purpose `guard` | "Tool call judged", with risk and P(asked). The call is allowed unless a `tool_guard` follows. |
| `decision` purpose `keep` | Compaction keep verdict on a file read |
| `decision` `ok=false` | System 1 ask failed (Intern retries it) |
| `tool_guard` layer `system1` / `rule` | "Tool call blocked by Drex" or "Blocked by guard rule (code)" |
| `nudge` `stuck_gate` | "Loop detected — agent nudged" (System 1). `wrap_up` is also System 1. Other reasons are harness nudges. |
| `state_writing` / `state` / `compaction` / `context_budget` | Context compaction, and context-window warnings |
| `run_end` `system1_unavailable` | Room **BLOCKED**, Intern process stopped |
| `run_end` `provider_error` | System 2 (DeepInfra) unavailable |

The Nace Observer card shows only real System 1 events. Until one arrives it shows "Waiting for Intern System 1
events." The raw payload of every event is kept, at `GET /sessions/{id}/raw` and in the timeline's "raw" toggles.

Citations get the **app's own check**: the citation is marked "verified" only when its `source_id` exists and its
excerpt appears in that source's text. This check is labelled as the app's, not Drex's.

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `NACE_API_KEY` | backend | System 1 (Drex Max v1.1, Nace console) |
| `DEEPINFRA_API_KEY` | backend | System 2 |
| `INTERN_SYSTEM1` | backend | default `drex-max-v1.1` |
| `INTERN_SYSTEM2` | backend | `deepinfra/<model>`; must appear in `intern models --json` once the key is set |
| `KEENABLE_API_KEY` | backend | optional research |
| `ADOBE_PDF_SERVICES_CLIENT_ID` (or `ADOBE_CLIENT_ID`), `ADOBE_PDF_SERVICES_CLIENT_SECRET` | backend | optional PDF export |
| `SPEECHMATICS_API_KEY` | backend | optional audio transcription |
| `FRONTEND_ORIGIN` | backend | CORS origin(s), comma-separated |
| `STUDYROOM_DATA_DIR` | backend | room folders, default `/tmp/studyroom` |
| `NEXT_PUBLIC_API_BASE_URL` | frontend | backend URL. This is the only thing the browser knows. |

Copy `.env.example` to `.env` at the repo root. The backend loads it. Put `NEXT_PUBLIC_API_BASE_URL` in
`web/.env.local`. Only Intern's own variables (`NACE_API_KEY`, `DEEPINFRA_API_KEY`, PATH, HOME, ...) are passed to
the Intern process. Partner keys are not.

## Install Intern and check models

```bash
pip install uv            # or: curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "intern-agent[workspace,ocr] @ git+https://github.com/nace-ai/intern"
intern --version          # this build was written against 0.30.1, RPC protocol 2
NACE_API_KEY=... DEEPINFRA_API_KEY=... intern models --json
```

`drex-max-v1.1` must appear under `system1`. **Without a valid `NACE_API_KEY`, `intern models` lists only
`drex-latest`.** That does not confirm Drex Max v1.1, and `POST /sessions` refuses to start the room. Pick
`INTERN_SYSTEM2` from the `deepinfra/...` ids under `system2`.

## Run locally

```bash
cd server && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

```bash
cd web && npm install && npm run dev        # http://localhost:3000
```

Checks:

```bash
cd web && npm run typecheck && npm run build
cd server && .venv/bin/python -m pytest -q tests
curl -s localhost:8000/health
```

## Happy-path test (text only)

1. Open http://localhost:3000. The status card shows the Intern version and which keys are set (booleans only).
2. Enter a course, then paste a page of notes into the notes box and click **Create study room**. The server checks
   the keys, checks `intern models --json` for `drex-max-v1.1`, spawns
   `intern rpc --system1 drex-max-v1.1 --system2 deepinfra/<model>`, and waits for `get_state` to answer.
3. In the room, ask "Can someone explain the main idea of my notes?". The turn goes to Intern through `prompt`, or
   through `follow_up` while a run is busy.
4. Watch the tiles go to *thinking*, then *speaking* (a waveform plays during browser speech). Tool calls and Drex's
   decisions appear in the Events tab and on the Observer card. Citation badges show ✓ when the app's check passes.
5. Click **Quiz me** and **Summarize**, then **Export to Adobe**. Without Adobe credentials you get
   "Adobe not configured" and the recap opens as HTML.

Errors show up verbatim. For example, with no keys, `POST /sessions` returns Intern's own stderr:
`intern: deepinfra needs an API key in $DEEPINFRA_API_KEY`. With only the DeepInfra key:
`intern: System 1 (drex-max-v1.1 from the console) needs a key in $NACE_API_KEY ...`.

## Sources and grounding

Source priority: handwritten notes > slides/PDFs > lecture transcript > Intern/Nace workspace grounding > Keenable
external research. External research is always labelled EXTERNAL and never overrides course material.

- **Adding sources:** on the landing page you can drop files (PDF, PPTX, TXT, MD, and PNG/JPG with OCR), add YouTube
  links and paste text, all before creating the room. A progress list shows how each source went. The room's Sources
  panel accepts the same files and links.
- **PowerPoint (.pptx):** python-pptx extracts slide text, tables and speaker notes as `[slide N]` blocks, and the
  original deck stays in `sources/`. For `.ppt` or Keynote, export as PPTX or PDF.
- **YouTube links:** the transcript comes from the video's own captions (published captions preferred,
  auto-generated accepted), via `youtube-transcript-api`. The title comes from YouTube's oEmbed endpoint. Captions are
  saved as timestamped `[mm:ss]` lines. If a video has no captions, you get an error and can paste a transcript
  instead. A transcript is never invented. `youtube-transcript-api` is an unofficial client, so YouTube may
  rate-limit or break it.
- Pasted text, TXT and MD always work.
- PDF: the server extracts text with pypdf (`S#-name.pdf.txt`) and keeps the original PDF in `sources/` for Intern's
  workspace index (`index_files` / `decision_files`).
- PNG/JPG: OCR runs through Intern's `[ocr]` extra (rapidocr), detected from the `intern` executable's own Python.
  If OCR is missing, the source is marked "OCR unavailable" and is never claimed as read.
- `sources/manifest.md` lists each source's id, name, type, priority, origin and file. Every turn sent to Intern
  names the current sources.

## Partner setup

- **Keenable**: Intern 0.30.1 does support remote HTTP MCP servers (`[[capabilities.mcp]]`, needs the `[mcp]` extra),
  but each entry has to name the exact remote tool names. I could not check Keenable's MCP tool names without a key,
  and a wrong name fails session setup. So research goes through a server adapter instead:
  `POST https://api.keenable.ai/v1/search` with `X-API-Key`. Results are saved as an EXTERNAL source file. System 2
  then reads that file through ordinary tool calls under Drex's guard. Without a key: "Keenable not configured".
- **Adobe PDF Services**: token → `/assets` → PUT a ZIP containing `index.html` → `/operation/htmltopdf` → poll →
  download. The PDF is served at `/sessions/{id}/artifacts/<name>`.
- **Speechmatics**: batch v2 (`/v2/jobs`, poll, transcript as txt). The Record button appears only when it's
  configured. Typed input stays the main path.
- **Speechmatics TTS** voices the personas when `SPEECHMATICS_API_KEY` is set. It uses the preview API
  (`POST https://preview.tts.speechmatics.com/generate/<voice>`, 16 kHz WAV) through the server's `POST /tts`,
  so the key stays on the server. Voices: Maya → `megan` (US female), Leo → `jack` (US male), Sam → `theo`
  (UK male), Priya → `sarah` (UK female). Markdown, URLs and hidden quiz answers are stripped before speaking. Each
  turn's clip starts fetching as soon as the turn arrives, so clips play back to back.
- **Meeting mode:**
  - Tiles show faces and the speaking waveform, with no text blocks.
  - A caption bar (CC) shows only the person speaking. Each line joins the Transcript tab once it has been said.
  - Speakers take turns with a 0.5s pause between clips, and the System 2 prompt asks for short spoken turns.
  - **Talk** records until you press **Send** (or Cancel); it never stops by itself. Then Speechmatics transcribes
    and the text is sent. When someone asks you a question, a "Your turn" card appears with a mic button. Talking or
    typing stops the group, so you can interrupt.
  - Only one open tab voices each turn, so two tabs on one room don't echo.
- **Browser SpeechSynthesis** is the fallback when Speechmatics isn't configured or a clip fails. The Voice on/off
  toggle mutes both. If neither is available, nothing breaks.

## Deploy (only after the local MVP works)

- **Render** (backend): `render.yaml` installs `requirements.txt`, uv and `intern-agent[workspace,ocr]`, then runs
  `uvicorn app:app --host 0.0.0.0 --port $PORT`. Set `NACE_API_KEY`, `DEEPINFRA_API_KEY`, `INTERN_SYSTEM2`, the
  optional partner keys, and `FRONTEND_ORIGIN=https://<your-vercel-app>`. Rooms live in memory and `/tmp`, so use one
  instance.
- **Vercel** (frontend): root directory `web`, env `NEXT_PUBLIC_API_BASE_URL=https://<render-service>.onrender.com`.

## Known limitations

- State is in memory: a backend restart loses rooms and stops their Intern processes.
- There's one Intern process per room and no limit on the number of rooms.
- The Keenable response shape is parsed loosely (`results` / `data` / `items`, then `title` / `url` / `snippet`),
  because I couldn't check it without a key.
- The default `INTERN_SYSTEM2` model id is a guess until it's checked against `intern models` with a DeepInfra key.
  The server refuses to start a room if the id isn't listed.
- If System 2 doesn't return JSON, the reply is shown as an unattributed "Study group" turn.
- No auth, billing, real-time voice or multi-user rooms.
