"""Study Room backend: FastAPI, in-memory rooms, one long-lived `intern rpc` process per room.

DeepInfra (System 2) writes the study discussion; Nace Drex Max v1.1 (System 1) supervises every turn and tool call
inside Intern. This server only relays, displays and stores. There is no mock or offline path: if Intern, a key, or
a partner service is missing, the endpoint says so."""

from __future__ import annotations

import asyncio
import html
import re
import json
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

import grounding  # noqa: E402
import normalizer  # noqa: E402
import overview  # noqa: E402
from adapters import adobe, keenable, speechmatics, youtube  # noqa: E402
from intern_runner import (  # noqa: E402
    SESSION_BUSY,
    SESSION_IDLE,
    SUPPORTED_PROTOCOL_VERSION,
    InternProcess,
    InternStartupError,
    RpcError,
    find_intern,
    has_module,
    intern_models,
    intern_python,
    intern_version,
)
from models import (  # noqa: E402
    CreateSessionBody,
    InternEvent,
    QuizBody,
    ResearchBody,
    Source,
    StudyRoom,
    StudyTurn,
    TextSourceBody,
    YoutubeBody,
    TurnBody,
    default_participants,
    new_id,
)

SYSTEM1 = os.environ.get("INTERN_SYSTEM1", "drex-max-v1.1")
SYSTEM2 = os.environ.get("INTERN_SYSTEM2", "deepinfra/Qwen/Qwen3-235B-A22B-Instruct-2507")
DATA_DIR = Path(os.environ.get("STUDYROOM_DATA_DIR", "/tmp/studyroom")).resolve()
TEMPLATES = ROOT / "intern_config"
INSTALL_HINT = 'uv tool install "intern-agent[workspace,ocr] @ git+https://github.com/nace-ai/intern"'
INTERN_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "NACE_API_KEY", "DREX_API_KEY", "DEEPINFRA_API_KEY",
                   "DEEPINFRA_BASE_URL", "NACE_BASE_URL")

app = FastAPI(title="Study Room API")
origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["*"],
                   allow_headers=["*"])


def error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


# ---------------------------------------------------------------- room runtime

class RoomRuntime:
    def __init__(self, room: StudyRoom, base: Path) -> None:
        self.room = room
        self.base = base
        self.home = base / "home"
        self.workspace = base / "workspace"
        self.artifacts = base / "artifacts"
        self.proc: InternProcess | None = None
        self.log: list[dict[str, Any]] = []  # replayable envelopes
        self.raw_events: list[Any] = []  # every raw Intern event, untouched
        self.overview: dict[str, Any] | None = None  # the latest structured final overview
        self.overview_stats: dict[str, int] = {}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.seq = 0
        self.mode_queue: deque[str] = deque()
        self.current_run: str | None = None
        self.idle = asyncio.Event()  # set while no Intern run is in progress
        self.idle.set()

    # ---- publishing
    def publish(self, type_: str, data: Any, persist: bool = True) -> None:
        self.seq += 1
        env = {"seq": self.seq, "type": type_, "data": data}
        if persist:
            self.log.append(env)
        for q in list(self.subscribers):
            q.put_nowait(env)

    def publish_room(self) -> None:
        self.publish("room", room_view(self))

    def emit(self, ev: InternEvent) -> None:
        if ev.raw_type in normalizer.STREAMING_TYPES:
            return  # token streams go out once, as "live" envelopes; the raw events stay in raw_events
        self.publish("intern_event", ev.model_dump(), persist=ev.kind != "system2_thinking")

    def participant(self, pid: str):  # noqa: ANN201
        return next((p for p in self.room.participants if p.id == pid), None)

    def set_ai_state(self, state: str) -> None:
        for p in self.room.participants:
            if p.kind == "ai":
                p.state = state  # type: ignore[assignment]

    # ---- Intern callbacks
    def on_event(self, raw: dict[str, Any]) -> None:
        self.raw_events.append(raw)
        t = raw.get("type")
        for ev in normalizer.normalize(raw):
            self.emit(ev)
            if ev.layer == "system1" and t in ("decision", "tool_guard", "nudge", "process_check"):
                self.room.latest_decision = {"title": ev.title, "detail": ev.detail, "kind": ev.kind,
                                             "severity": ev.severity, "at": ev.created_at}
                self.room.drex_status = {
                    "system1_blocked": "Blocked a tool call",
                    "system1_warning": "Warning: turn may be stuck",
                    "system1_nudge": "Nudged the agent",
                }.get(ev.kind, "Supervising")
        changed = True
        if t == "run_start":
            self.room.busy = True
            self.room.status = "running"
            self.current_run = raw.get("run_id")
            self.room.current_mode = self.mode_queue.popleft() if self.mode_queue else "discussion"
            self.room.protocol_version = raw.get("protocol_version")
            if raw.get("protocol_version") not in (None, SUPPORTED_PROTOCOL_VERSION):
                self.room.status_detail = f"Unsupported Intern RPC protocol {raw.get('protocol_version')}"
            self.set_ai_state("thinking")
            hp = self.participant("human")
            if hp:
                hp.state = "listening"
        elif t in ("text_delta", "reasoning_delta"):
            self.publish("live", {"kind": t, "text": raw.get("text", "")}, persist=False)
            changed = False
        elif t == "message":
            msg = raw.get("message") or {}
            if msg.get("role") == "assistant" and not msg.get("tool_calls") and (msg.get("text") or "").strip():
                self.add_assistant_text(msg["text"])
            else:
                changed = False
        elif t == "tool_guard":
            pass
        elif t == "run_end":
            self.room.busy = False
            self.idle.set()
            reason = raw.get("reason")
            if reason == "system1_unavailable":
                self.block("System 1 (Drex) unavailable: " + str(raw.get("detail") or ""))
                asyncio.get_running_loop().create_task(self.proc.stop()) if self.proc else None
            elif reason == "provider_error":
                self.room.status = "ready"
                self.room.status_detail = "System 2 (DeepInfra) unavailable: " + str(raw.get("detail") or "")
                self.set_ai_state("idle")
            else:
                self.room.status = "ready"
                self.room.status_detail = None
                for p in self.room.participants:
                    if p.kind == "ai" and p.state == "thinking":
                        p.state = "idle"
            hp = self.participant("human")
            if hp:
                hp.state = "idle"
        else:
            changed = t in ("decision", "tool_guard", "nudge")
        if changed:
            self.publish_room()

    def add_assistant_text(self, text: str) -> None:
        turns = grounding.parse_system2(text, self.room.current_mode, self.current_run)
        if self.room.current_mode == "overview":
            ov = overview.extract(text)
            if ov:
                self.overview = overview.clean(ov)
                self.overview_stats = overview.verify(self.overview, self.room.sources, self.workspace)
                self.emit(InternEvent(kind="grounding_check", layer="ui", severity="success",
                                      title=f"Final overview written · {self.overview_stats['verified']}/"
                                            f"{self.overview_stats['citations']} citations verified (app check)",
                                      raw_payload=self.overview))
                # Keep only the spoken line(s); the JSON document itself is not a chat turn.
                turns = [t for t in turns if t.speaker != "group"] or [StudyTurn(
                    speaker="maya", message="Our study guide is ready. It's in the recap.", mode="overview",
                    run_id=self.current_run)]
            else:
                self.emit(InternEvent(kind="grounding_check", layer="ui", severity="warning",
                                      title="Final overview reply had no overview object; the recap falls back to the "
                                            "discussion"))
        for turn in turns:
            grounding.verify_citations(turn, self.room.sources, self.workspace)
            self.room.turns.append(turn)
            self.publish("turn", turn.model_dump())
            for c in turn.citations:
                self.emit(InternEvent(kind="source_citation", layer="ui",
                                      severity="success" if c.app_check == "verified" else "warning",
                                      title=f"Citation {c.source_id or '?'} — app check: {c.app_check}",
                                      detail=f"{c.location} · “{c.excerpt[:160]}”", raw_payload=c.model_dump()))
            p = self.participant(turn.speaker)
            if p:
                for other in self.room.participants:
                    if other.kind == "ai" and other.id != p.id and other.state != "blocked":
                        other.state = "listening"
                p.state = "speaking"
                p.latest_message = turn.message
                p.latest_citations = [c.model_dump() for c in turn.citations]

    def on_malformed(self, text: str) -> None:
        self.raw_events.append({"malformed": text})
        self.emit(normalizer.malformed(text))

    def on_exit(self, code: int | None, stderr: str) -> None:
        self.room.busy = False
        self.idle.set()
        self.emit(InternEvent(kind="session_error", layer="ui", severity="error",
                              title=f"Intern process exited (code {code})", detail=stderr[-2000:] or None,
                              raw_payload={"exit_code": code, "stderr": stderr}))
        if self.room.status != "blocked":
            self.room.status = "exited"
            self.room.status_detail = f"Intern process exited (code {code}). {stderr.splitlines()[-1] if stderr else ''}"
            self.set_ai_state("idle")
        self.publish_room()

    def block(self, why: str) -> None:
        self.room.status = "blocked"
        self.room.status_detail = why
        self.room.drex_status = "Unavailable — room blocked"
        for p in self.room.participants:
            if p.kind != "human":
                p.state = "blocked"

    # ---- talking to Intern
    async def send(self, text: str, mode: str) -> dict[str, Any]:
        if self.room.status == "blocked":
            raise error(409, "room_blocked", self.room.status_detail or "Room is blocked")
        if not self.proc or not self.proc.alive:
            raise error(409, "intern_exited", "Intern process exited", intern_stderr=self.proc.stderr_text() if self.proc else "")
        method = "follow_up" if self.room.busy else "prompt"
        try:
            result = await self.proc.request(method, {"text": text})
        except RpcError as e:
            if e.code == SESSION_BUSY:
                method = "follow_up"
                result = await self.proc.request(method, {"text": text})
            elif e.code == SESSION_IDLE:
                method = "prompt"
                result = await self.proc.request(method, {"text": text})
            else:
                raise error(502, "intern_rpc_error", e.message, code=e.code) from e
        if method == "prompt" or (isinstance(result, dict) and result.get("started")):
            self.mode_queue.append(mode)
        self.room.busy = True
        self.idle.clear()
        self.emit(InternEvent(kind="system2_started", layer="ui", severity="debug", title=f"Sent to Intern via {method}",
                              raw_payload={"method": method, "result": result}))
        return {"method": method, "result": result}


ROOMS: dict[str, RoomRuntime] = {}


def room_view(rt: RoomRuntime) -> dict[str, Any]:
    d = rt.room.model_dump(exclude={"turns"})
    d["ocr_available"] = OCR_AVAILABLE
    return d


def get_room(room_id: str) -> RoomRuntime:
    rt = ROOMS.get(room_id)
    if not rt:
        raise error(404, "not_found", "No such room")
    return rt


# ---------------------------------------------------------------- environment checks

INTERN_EXE = find_intern()
INTERN_VERSION = intern_version(INTERN_EXE) if INTERN_EXE else None
INTERN_PY = intern_python(INTERN_EXE) if INTERN_EXE else None
OCR_AVAILABLE = has_module(INTERN_PY, "rapidocr_onnxruntime")


def keys_present() -> dict[str, bool]:
    return {
        "NACE_API_KEY": bool(os.environ.get("NACE_API_KEY") or os.environ.get("DREX_API_KEY")),
        "DEEPINFRA_API_KEY": bool(os.environ.get("DEEPINFRA_API_KEY")),
        "KEENABLE_API_KEY": keenable.configured(),
        "ADOBE_PDF_SERVICES": adobe.configured(),
        "SPEECHMATICS_API_KEY": speechmatics.configured(),
    }


def intern_env(home: Path) -> dict[str, str]:
    env = {k: os.environ[k] for k in INTERN_ENV_KEYS if k in os.environ}
    env["INTERN_HOME"] = str(home)
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": INTERN_EXE is not None,
        "intern": {"found": INTERN_EXE is not None, "path": INTERN_EXE, "version": INTERN_VERSION,
                   "install": None if INTERN_EXE else INSTALL_HINT, "ocr_extra": OCR_AVAILABLE,
                   "supported_protocol_version": SUPPORTED_PROTOCOL_VERSION},
        "system1": SYSTEM1,
        "system2": SYSTEM2,
        "keys": keys_present(),  # booleans only, never values
        "integrations": {"keenable": keenable.configured(), "adobe": adobe.configured(),
                         "speechmatics": speechmatics.configured(), "speechmatics_tts": speechmatics.configured()},
        "rooms": len(ROOMS),
    }


# ---------------------------------------------------------------- sessions

@app.post("/sessions")
async def create_session(body: CreateSessionBody) -> dict[str, Any]:
    if not INTERN_EXE:
        raise error(503, "intern_missing", f"Intern executable not found on PATH. Install it: {INSTALL_HINT}")
    room_id = new_id("room")
    base = DATA_DIR / room_id
    home, workspace = base / "home", base / "workspace"
    (workspace / "sources").mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    (base / "artifacts").mkdir(exist_ok=True)
    for name in ("SYSTEM2.md", "SYSTEM1.md", "config.toml"):
        shutil.copy(TEMPLATES / name, home / name)
    shutil.copy(TEMPLATES / "AGENTS.md", workspace / "AGENTS.md")

    room = StudyRoom(id=room_id, title=body.title or "Study Room", course=body.course, system1=SYSTEM1,
                     system2=SYSTEM2, intern_version=INTERN_VERSION,
                     participants=default_participants(body.human_name))
    rt = RoomRuntime(room, base)
    grounding.write_manifest(workspace, [])
    env = intern_env(home)

    keys = keys_present()
    missing = [k for k in ("NACE_API_KEY", "DEEPINFRA_API_KEY") if not keys[k]]
    if not missing:
        models, models_err = await asyncio.to_thread(intern_models, INTERN_EXE, env)
        if models is None:
            raise error(502, "intern_models_failed", "`intern models --json` failed", intern_stderr=models_err)
        s1_ids = [m.get("id") for m in models.get("system1", [])]
        if SYSTEM1 not in s1_ids:
            raise error(502, "drex_unavailable", f"Drex Max v1.1 unavailable: {SYSTEM1} is not in the System 1 list "
                        "from `intern models`. Intern lists only drex-latest when NACE_API_KEY is missing or not "
                        "accepted by the Nace console — check the key.",
                        system1_available=s1_ids, intern_stderr=models_err)
        s2_ids = [m.get("id") for m in models.get("system2", [])]
        if s2_ids and SYSTEM2 not in s2_ids:
            deepinfra = [i for i in s2_ids if str(i).startswith("deepinfra/")]
            raise error(502, "system2_unavailable", f"{SYSTEM2} is not in the System 2 list from `intern models`",
                        deepinfra_available=deepinfra[:50])

    args = ["rpc", "--system1", SYSTEM1, "--system2", SYSTEM2]
    proc = InternProcess(INTERN_EXE, args, workspace, env, rt.on_event, rt.on_malformed, rt.on_exit)
    rt.proc = proc
    room.intern_args = proc.argv
    try:
        state = await proc.start()
    except InternStartupError as e:
        # Intern's own stderr, word for word.
        raise error(502, "intern_startup_failed", str(e), intern_stderr=e.stderr, exit_code=e.exit_code,
                    missing_keys=missing, intern_args=proc.argv) from e
    room.intern_pid = proc.proc.pid if proc.proc else None
    room.status = "ready"
    ROOMS[room_id] = rt
    rt.emit(InternEvent(kind="system1_started", layer="ui", severity="success",
                        title="Intern session up (get_state answered)", detail=" ".join(proc.argv),
                        raw_payload=state))
    rt.publish_room()
    return {"room": room_view(rt), "state": state}


@app.get("/sessions/{room_id}")
def get_session(room_id: str) -> dict[str, Any]:
    rt = get_room(room_id)
    return {"room": room_view(rt), "turns": [t.model_dump() for t in rt.room.turns]}


@app.get("/sessions/{room_id}/raw")
def raw_events(room_id: str) -> list[Any]:
    return get_room(room_id).raw_events


@app.get("/sessions/{room_id}/events")
async def events(room_id: str, request: Request) -> StreamingResponse:
    rt = get_room(room_id)
    after = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    replay = [e for e in rt.log if e["seq"] > after]
    snapshot = {"seq": rt.seq, "type": "room", "data": room_view(rt)}
    rt.subscribers.add(q)

    def fmt(env: dict[str, Any]) -> str:
        return f"id: {env['seq']}\nevent: {env['type']}\ndata: {json.dumps(env, default=str)}\n\n"

    async def gen():  # noqa: ANN202
        try:
            last = after
            for env in replay:
                last = env["seq"]
                yield fmt(env)
            yield fmt(snapshot)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    env = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if env["seq"] <= last:
                    continue
                last = env["seq"]
                yield fmt(env)
        finally:
            rt.subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- sources

def next_source_id(rt: RoomRuntime) -> str:
    return f"S{len(rt.room.sources) + 1}"


def register_source(rt: RoomRuntime, src: Source) -> dict[str, Any]:
    rt.room.sources.append(src)
    grounding.write_manifest(rt.workspace, rt.room.sources)
    rt.emit(InternEvent(kind="grounding_check", layer="ui",
                        severity="success" if src.readiness == "ready" else "warning",
                        title=f"Source {src.id} added: {src.name} ({src.readiness})",
                        detail=src.readiness_detail or src.priority_label, raw_payload=src.model_dump()))
    rt.publish_room()
    return src.model_dump()


@app.post("/sessions/{room_id}/sources")
async def add_source(room_id: str, request: Request) -> dict[str, Any]:
    rt = get_room(room_id)
    ctype = request.headers.get("content-type", "")
    sid = next_source_id(rt)
    sdir = rt.workspace / "sources"
    if ctype.startswith("application/json"):
        body = TextSourceBody.model_validate(await request.json())
        if not body.text.strip():
            raise error(400, "empty_source", "Pasted text is empty")
        rel = f"sources/{sid}-{grounding.slug(body.name)}.md"
        text = f"# {body.name}\n\n{body.text.strip()}\n"
        (rt.workspace / rel).write_text(text)
        return register_source(rt, grounding.make_source(sid, body.name, body.kind, rel, text))

    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise error(400, "no_file", "Send JSON {name, kind, text} or multipart with a `file` field")
    filename = getattr(upload, "filename", "upload") or "upload"
    kind = str(form.get("kind") or "")
    name = str(form.get("name") or filename)
    data = await upload.read()  # type: ignore[union-attr]
    ext = Path(filename).suffix.lower()
    base_name = f"{sid}-{grounding.slug(Path(filename).stem)}"

    if ext in grounding.TEXT_EXT:
        rel = f"sources/{base_name}{ext}"
        text = data.decode("utf-8", errors="replace")
        (rt.workspace / rel).write_text(text)
        return register_source(rt, grounding.make_source(sid, name, kind or "notes", rel, text))

    if ext == ".pdf":
        pdf_rel = f"sources/{base_name}.pdf"
        (rt.workspace / pdf_rel).write_bytes(data)
        txt_rel = f"sources/{base_name}.pdf.txt"
        try:
            text = await asyncio.to_thread(grounding.extract_pdf, rt.workspace / pdf_rel)
        except Exception as e:  # noqa: BLE001
            text, why = "", f"File extraction failed: {type(e).__name__}: {e}"
        else:
            why = None if text.strip() else "File extraction failed: no text layer (scanned PDF?)"
        (rt.workspace / txt_rel).write_text(text)
        src = grounding.make_source(sid, name, kind or "pdf", txt_rel, text)
        src.extra_files = [pdf_rel]
        if why:
            src.readiness, src.readiness_detail = "failed", why + " — the PDF is still in the workspace for Intern's index."
        return register_source(rt, src)

    if ext in grounding.IMAGE_EXT:
        img_rel = f"sources/{base_name}{ext}"
        (rt.workspace / img_rel).write_bytes(data)
        if not OCR_AVAILABLE or not INTERN_PY:
            src = grounding.make_source(sid, name, kind or "handwritten", img_rel, "")
            src.readiness, src.readiness_detail = "ocr_unavailable", "OCR unavailable: the image was NOT read."
            return register_source(rt, src)
        txt_rel = f"sources/{base_name}.ocr.txt"
        try:
            text = await asyncio.to_thread(grounding.ocr_image, INTERN_PY, rt.workspace / img_rel)
        except Exception as e:  # noqa: BLE001
            src = grounding.make_source(sid, name, kind or "handwritten", img_rel, "")
            src.readiness, src.readiness_detail = "failed", f"OCR failed: {e}"
            return register_source(rt, src)
        (rt.workspace / txt_rel).write_text(text)
        src = grounding.make_source(sid, name, kind or "handwritten", txt_rel, text)
        src.extra_files = [img_rel]
        src.readiness_detail = "OCR by Intern's [ocr] engine (rapidocr)"
        if not text.strip():
            src.readiness, src.readiness_detail = "failed", "OCR found no text in the image."
        return register_source(rt, src)

    if ext == ".pptx":
        deck_rel = f"sources/{base_name}.pptx"
        (rt.workspace / deck_rel).write_bytes(data)
        txt_rel = f"sources/{base_name}.pptx.txt"
        try:
            text = await asyncio.to_thread(grounding.extract_pptx, rt.workspace / deck_rel)
        except Exception as e:  # noqa: BLE001
            text, why = "", f"File extraction failed: {type(e).__name__}: {e}"
        else:
            why = None if text.strip() else "File extraction failed: the slides have no text (image-only deck?)"
        (rt.workspace / txt_rel).write_text(text)
        src = grounding.make_source(sid, name, kind or "slides", txt_rel, text)
        src.extra_files = [deck_rel]
        if why:
            src.readiness, src.readiness_detail = "failed", why + " Export the deck as PDF and upload that instead."
        else:
            src.readiness_detail = f"{text.count('[slide ')} slides, including speaker notes"
        return register_source(rt, src)

    if ext in (".ppt", ".key"):
        raise error(415, "unsupported_type", f"{ext} files can't be read. Save the deck as .pptx or export it as PDF.")
    raise error(415, "unsupported_type", f"Unsupported file type {ext or '(none)'}: use PDF, PPTX, TXT, MD, PNG or JPG")


@app.post("/sessions/{room_id}/sources/youtube")
async def add_youtube(room_id: str, body: YoutubeBody) -> dict[str, Any]:
    """A YouTube lecture as a transcript source, from the video's own captions. Never an invented transcript."""
    rt = get_room(room_id)
    vid = youtube.video_id(body.url)
    if not vid:
        raise error(400, "bad_youtube_url", "That doesn't look like a YouTube video link.")
    try:
        cap = await asyncio.to_thread(youtube.transcript, vid)
    except youtube.NoTranscript as e:
        raise error(422, "no_transcript", f"{e} You can paste the transcript as text instead.") from e
    meta = await asyncio.to_thread(youtube.title, vid)
    name = body.name or (f"YouTube: {meta['title']}" if meta["title"] else f"YouTube video {vid}")
    sid = next_source_id(rt)
    rel = f"sources/{sid}-youtube-{vid}.md"
    header = (f"# {name}\n\nSource: https://www.youtube.com/watch?v={vid}"
              f"{' · ' + meta['author'] if meta['author'] else ''}\n"
              f"Transcript: YouTube {'auto-generated' if cap['auto_generated'] else 'published'} captions "
              f"({cap['language']}), timestamps [mm:ss].\n\n")
    text = header + "\n".join(cap["lines"]) + "\n"
    (rt.workspace / rel).write_text(text)
    src = grounding.make_source(sid, name, "transcript", rel, text)
    src.readiness_detail = (f"{'Auto-generated' if cap['auto_generated'] else 'Published'} YouTube captions · "
                            f"{len(cap['lines'])} timestamped lines")
    return register_source(rt, src)


# ---------------------------------------------------------------- turns

def sources_line(rt: RoomRuntime) -> str:
    if not rt.room.sources:
        return "No course sources have been added yet. Say so if the question needs them."
    rows = [f'- {s.id} "{s.name}" — {s.kind}, {s.priority_label}, {s.origin.upper()}, file {s.file_path}'
            f"{' (' + s.readiness + ')' if s.readiness != 'ready' else ''}"
            for s in sorted(rt.room.sources, key=lambda s: (s.priority, s.id))]
    return "Current sources (full list in sources/manifest.md):\n" + "\n".join(rows)


def human_name(rt: RoomRuntime) -> str:
    p = rt.participant("human")
    return p.name if p else "the human"


DEFAULT_TAIL = ("Reply as the study group, grounded in the sources, as the JSON array of turns described in your "
                "instructions. Excerpts must be verbatim from the source files.")


def wrap(rt: RoomRuntime, mode: str, instruction: str, tail: str = DEFAULT_TAIL) -> str:
    return (f"[Study room · course: {rt.room.course or rt.room.title} · mode: {mode}]\n{sources_line(rt)}\n\n"
            f"{instruction}\n\n{tail}")


@app.post("/sessions/{room_id}/turns")
async def post_turn(room_id: str, body: TurnBody) -> dict[str, Any]:
    rt = get_room(room_id)
    text = body.text.strip()
    if not text:
        raise error(400, "empty", "Message is empty")
    turn = StudyTurn(speaker="human", message=text)
    rt.room.turns.append(turn)
    rt.publish("turn", turn.model_dump())
    prompt = wrap(rt, "discussion", f'{human_name(rt)} (the human student, an equal member) says:\n"""\n{text}\n"""')
    return {"turn": turn.model_dump(), **await rt.send(prompt, "discussion")}


@app.post("/sessions/{room_id}/quiz")
async def quiz(room_id: str, body: QuizBody) -> dict[str, Any]:
    rt = get_room(room_id)
    topic = body.topic.strip() or "the most important ideas in the current sources"
    n = max(1, min(body.count, 6))
    human_turn(rt, f"Quiz me on {topic}.", "quiz")
    instr = (f"{human_name(rt)} asks for a spoken quiz on: {topic}. This is a {n}-question quiz asked ONE question at a "
             "time. Priya says she'll quiz them and asks question 1 out loud, drawn only from the course sources, with its "
             "citation (verbatim excerpt). Keep the expected answer out of what is spoken: put it after 'Answer:' at the "
             "very end of Priya's message (it is stripped before speaking). Sam may add one short line confirming the "
             "question is answerable from the sources. Use next_action \"quiz\" and put the question in "
             f"question_for_human. When {human_name(rt)} answers, Priya says if it is right, briefly why, then asks the next.")
    return await rt.send(wrap(rt, "quiz", instr), "quiz")


def human_turn(rt: RoomRuntime, message: str, mode: str) -> StudyTurn:
    turn = StudyTurn(speaker="human", message=message, mode=mode)
    rt.room.turns.append(turn)
    rt.publish("turn", turn.model_dump())
    return turn


SUMMARY_INSTR = ("{name} asks for a summary. Maya gives the key points of the discussion and the sources, each with a "
                 "citation; Sam names what the sources do not cover or what is still uncertain; Priya ends with "
                 "concrete next steps. Use next_action \"summarize\".")


async def request_summary(rt: RoomRuntime, final: bool = False) -> dict[str, Any]:
    if final:  # a structured study guide (overview.py), not persona chatter
        human_turn(rt, "Put together the final study guide.", "overview")
        prompt = wrap(rt, "overview", overview.PROMPT.format(name=human_name(rt)), tail=overview.TAIL)
        return await rt.send(prompt, "overview")
    human_turn(rt, "Summarize what we have so far.", "summary")
    return await rt.send(wrap(rt, "summary", SUMMARY_INSTR.format(name=human_name(rt))), "summary")


@app.post("/sessions/{room_id}/summarize")
async def summarize(room_id: str) -> dict[str, Any]:
    return await request_summary(get_room(room_id))


@app.post("/sessions/{room_id}/research")
async def research(room_id: str, body: ResearchBody) -> dict[str, Any]:
    rt = get_room(room_id)
    if not keenable.configured():
        raise error(400, "keenable_not_configured", "Keenable not configured (set KEENABLE_API_KEY)")
    try:
        result = await keenable.search(body.query)
    except Exception as e:  # noqa: BLE001
        rt.emit(InternEvent(kind="keenable_search", layer="ui", severity="error", title="Keenable search failed",
                            detail=str(e)))
        raise error(502, "keenable_failed", str(e)) from e
    sid = next_source_id(rt)
    rel = f"sources/{sid}-external-{grounding.slug(body.query)}.md"
    md = keenable.to_markdown(result)
    (rt.workspace / rel).write_text(md)
    src = grounding.make_source(sid, f"EXTERNAL: {body.query}", "external", rel, md, origin="external")
    register_source(rt, src)
    rt.emit(InternEvent(kind="keenable_search", layer="ui", severity="info",
                        title=f"Keenable search: {body.query} ({len(result['results'])} results) → {sid}",
                        raw_payload={"query": body.query, "results": result["results"]}))
    human_turn(rt, f"Research concept: {body.query}", "research")
    instr = (f"{human_name(rt)} asked for external research on: {body.query}. The results are saved as {sid} (EXTERNAL, "
             "lowest priority). Maya compares them with the course sources; Sam flags any conflict, and course "
             "material wins. Label every use of the external source as EXTERNAL.")
    sent = await rt.send(wrap(rt, "research", instr), "research")
    return {"source": src.model_dump(), "results": result["results"], **sent}


# ---------------------------------------------------------------- Adobe export

def build_recap(rt: RoomRuntime) -> dict[str, Any]:
    if rt.overview:
        return {"structured": True, "title": rt.room.title, "course": rt.room.course, "overview": rt.overview,
                "stats": rt.overview_stats, "generated_at": time.strftime("%B %d, %Y"),
                "sources": [{"id": s.id, "name": s.name, "type": s.kind, "priority": s.priority_label,
                             "origin": s.origin} for s in rt.room.sources]}
    ai = [t for t in rt.room.turns if t.speaker not in ("human",)]
    summaries = [t for t in ai if t.mode == "summary"]
    # The final overview is the most recent summary run only; older summaries are superseded.
    last_run = summaries[-1].run_id if summaries else None
    summary = [t for t in summaries if t.run_id == last_run] or ai[-6:]
    verified = [{"turn": t.speaker, **c.model_dump()} for t in ai for c in t.citations if c.app_check == "verified"]
    quiz_items = [t.message for t in ai if t.mode == "quiz" or t.next_action == "quiz"]
    next_steps = [t.message for t in summary if t.speaker == "priya"] or \
                 [t.question_for_human for t in ai if t.question_for_human]
    return {
        "title": rt.room.title, "course": rt.room.course,
        "sources": [{"id": s.id, "name": s.name, "type": s.kind, "priority": s.priority_label, "origin": s.origin}
                    for s in rt.room.sources],
        "key_points": [{"speaker": t.speaker, "message": t.message} for t in summary
                       if not (t.speaker == "priya" and next_steps and t.message in next_steps)],
        "verified_citations": verified, "quiz_items": quiz_items, "next_steps": next_steps,
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
    }


def md(text: str) -> str:
    """Minimal, safe markdown for the recap: escape first, then **bold**, *italic*, list lines and line breaks."""
    out = html.escape(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?m)^\s*[-•]\s+", "&bull; ", out)
    return out.replace("\n", "<br>")


def recap_html(r: dict[str, Any]) -> str:
    if r.get("structured"):
        return overview.render_html(r["overview"], r)
    e = html.escape
    li = lambda items: "".join(f"<li>{x}</li>" for x in items) or "<li><em>None yet.</em></li>"  # noqa: E731
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(r['title'])} recap</title>
<style>body{{font-family:Helvetica,Arial,sans-serif;margin:40px;color:#1b1f24}}h1{{margin-bottom:0}}
.sub{{color:#666;margin-top:4px}}h2{{border-bottom:2px solid #1a73e8;padding-bottom:4px;margin-top:28px}}
blockquote{{color:#444;border-left:3px solid #ccc;margin:4px 0;padding-left:10px}}.tag{{font-size:11px;color:#1a73e8}}</style>
</head><body><h1>{e(r['title'])}</h1><div class="sub">{e(r['course'])} · Study recap · {e(r['generated_at'])}</div>
<h2>Sources</h2><ul>{li(f"<b>{e(s['id'])}</b> {e(s['name'])} <span class='tag'>{e(s['priority'])}</span>" for s in r['sources'])}</ul>
<h2>Final overview</h2><ul>{li(f"<b>{e(k['speaker'].title())}:</b><br>{md(k['message'])}" for k in r['key_points'])}</ul>
<h2>Verified citations <span class="tag">(app check: excerpt found in source)</span></h2>
<ul>{li(f"<b>{e(c['source_id'])}</b> {e(c['location'])}<blockquote>{e(c['excerpt'])}</blockquote>" for c in r['verified_citations'])}</ul>
<h2>Quiz items</h2><ol>{li(md(q) for q in r['quiz_items'])}</ol>
<h2>Next steps</h2><ul>{li(md(n) for n in r['next_steps'] if n)}</ul>
<p class="sub">Discussion by System 2 (DeepInfra), supervised by Nace Drex Max v1.1 (System 1) via Intern.</p>
</body></html>"""


OVERVIEW_TIMEOUT_S = 300.0


def needs_final_overview(rt: RoomRuntime) -> bool:
    """A fresh overview is needed when the group has spoken and the room's last turn is not already a summary."""
    turns = rt.room.turns
    if not any(t.speaker != "human" for t in turns):
        return False
    last = turns[-1]
    return not (last.speaker != "human" and last.mode == "overview" and rt.overview is not None)


@app.post("/sessions/{room_id}/export/adobe")
async def export_adobe(room_id: str, fresh: bool = True) -> JSONResponse:
    """Final overview → Adobe PDF. With `fresh`, first asks the group (same Drex-supervised session) for a final
    overview and waits for that run to end, so the recap always closes on an up-to-date summary."""
    rt = get_room(room_id)
    if fresh and rt.room.status not in ("blocked", "exited"):
        try:
            await asyncio.wait_for(rt.idle.wait(), OVERVIEW_TIMEOUT_S)  # let a run in progress finish first
        except asyncio.TimeoutError as e:
            raise error(504, "overview_timeout", "The discussion in progress did not finish in time.") from e
    if fresh and needs_final_overview(rt) and rt.room.status not in ("blocked", "exited"):
        try:
            rt.emit(InternEvent(kind="adobe_artifact", layer="ui", title="Asking the group for the final overview"))
            await request_summary(rt, final=True)
            await asyncio.wait_for(rt.idle.wait(), OVERVIEW_TIMEOUT_S)
            if rt.overview is None and rt.room.status not in ("blocked", "exited"):
                # One re-ask: the reply could not be read as the study-guide JSON even after repair.
                rt.emit(InternEvent(kind="adobe_artifact", layer="ui", severity="warning",
                                    title="Study guide JSON unreadable; asking System 2 to resend it once"))
                await rt.send(wrap(rt, "overview", "Your last reply could not be read as valid JSON, so the study guide "
                                   "was lost. Resend the SAME study guide as ONE valid JSON object in exactly the format "
                                   "described before: every key in double quotes, the comparison with both \"columns\" and "
                                   "\"rows\", no trailing commas, no text outside the JSON.", tail=overview.TAIL), "overview")
                await asyncio.wait_for(rt.idle.wait(), OVERVIEW_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise error(504, "overview_timeout", "The final overview did not finish in time; try Export again.") from e
        if rt.room.status in ("blocked", "exited"):
            raise error(409, "room_" + rt.room.status, rt.room.status_detail or "Room stopped during the overview")
    recap = build_recap(rt)
    page = recap_html(recap)
    if not adobe.configured():
        return JSONResponse({"ok": False, "error": "Adobe not configured", "recap": recap, "recap_html": page})
    try:
        pdf = await adobe.html_to_pdf(page)
    except Exception as e:  # noqa: BLE001
        rt.emit(InternEvent(kind="adobe_artifact", layer="ui", severity="error", title="Adobe export failed",
                            detail=str(e)))
        return JSONResponse({"ok": False, "error": f"Adobe export failed: {e}", "recap": recap, "recap_html": page},
                            status_code=502)
    name = f"recap-{int(time.time())}.pdf"
    (rt.artifacts / name).write_bytes(pdf)
    art = {"name": name, "url": f"/sessions/{room_id}/artifacts/{name}", "bytes": len(pdf)}
    rt.room.artifacts.append(art)
    rt.emit(InternEvent(kind="adobe_artifact", layer="ui", severity="success",
                        title=f"Adobe PDF recap ready ({len(pdf) // 1024} KB)", raw_payload=art))
    rt.publish_room()
    return JSONResponse({"ok": True, "artifact": art, "recap": recap})


@app.get("/sessions/{room_id}/artifacts/{name}")
def artifact(room_id: str, name: str, download: bool = False) -> FileResponse:
    """Shown in the browser's PDF viewer by default (inline); `?download=1` saves it as a file."""
    rt = get_room(room_id)
    path = (rt.artifacts / name).resolve()
    if path.parent != rt.artifacts.resolve() or not path.exists():
        raise error(404, "not_found", "No such artifact")
    nice = re.sub(r"[^A-Za-z0-9]+", "-", f"Study guide {rt.room.course or rt.room.title}").strip("-")[:80] + ".pdf"
    return FileResponse(path, media_type="application/pdf", filename=nice,
                        content_disposition_type="attachment" if download else "inline")


# ---------------------------------------------------------------- Speechmatics

@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict[str, Any]:
    if not speechmatics.configured():
        raise error(400, "speechmatics_not_configured", "Speechmatics not configured (set SPEECHMATICS_API_KEY)")
    try:
        text = await speechmatics.transcribe(file.filename or "audio.webm", await file.read(),
                                             file.content_type or "application/octet-stream")
    except Exception as e:  # noqa: BLE001
        raise error(502, "speechmatics_failed", str(e)) from e
    return {"text": text}


class TtsBody(BaseModel):
    speaker: str
    text: str


def speakable(text: str) -> str:
    """Plain text for a voice: drop markdown markers, URLs and a quiz's hidden answers."""
    text = re.sub(r"Answers?:[\s\S]*$", "", text, flags=re.I)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>|]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


@app.post("/tts")
async def tts(body: TtsBody) -> Response:
    """Speechmatics text-to-speech in the persona's own voice; returns audio/wav. The key never leaves the server."""
    if not speechmatics.configured():
        raise error(400, "speechmatics_not_configured", "Speechmatics not configured (set SPEECHMATICS_API_KEY)")
    voice = speechmatics.PERSONA_VOICES.get(body.speaker.lower())
    if not voice:
        raise error(400, "unknown_speaker", f"No voice for speaker {body.speaker!r}")
    text = speakable(body.text)
    if not text:
        raise error(400, "empty", "Nothing to say")
    try:
        audio = await speechmatics.synthesize(text, voice)
    except Exception as e:  # noqa: BLE001
        raise error(502, "speechmatics_tts_failed", str(e)) from e
    return Response(audio, media_type="audio/wav", headers={"X-Voice": voice, "Cache-Control": "no-store"})


@app.on_event("shutdown")
async def shutdown() -> None:
    for rt in ROOMS.values():
        if rt.proc:
            await rt.proc.stop()
