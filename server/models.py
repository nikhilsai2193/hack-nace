"""In-memory data model for Study Room. Nothing here is persisted beyond the process lifetime."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


ParticipantKind = Literal["human", "ai", "observer"]
ParticipantState = Literal["idle", "listening", "thinking", "speaking", "blocked"]
RoomStatus = Literal["starting", "ready", "running", "blocked", "error", "exited"]
SourceKind = Literal["handwritten", "notes", "slides", "pdf", "transcript", "external", "text"]
Severity = Literal["debug", "info", "success", "warning", "error"]
EventLayer = Literal["system1", "system2", "tool", "ui"]

# Lower number = higher priority. Course material always outranks external research.
SOURCE_PRIORITY: dict[str, tuple[int, str]] = {
    "handwritten": (1, "P1 · handwritten notes"),
    "notes": (1, "P1 · notes"),
    "slides": (2, "P2 · slides"),
    "pdf": (2, "P2 · PDF"),
    "text": (2, "P2 · course text"),
    "transcript": (3, "P3 · lecture transcript"),
    "external": (5, "P5 · EXTERNAL research (never overrides course material)"),
}


class Participant(BaseModel):
    id: str
    name: str
    role: str
    kind: ParticipantKind
    initials: str
    state: ParticipantState = "idle"
    latest_message: str | None = None
    latest_citations: list[dict[str, Any]] = Field(default_factory=list)


class Source(BaseModel):
    id: str  # S1, S2, ... short ids System 2 can cite
    name: str
    kind: SourceKind
    priority: int
    priority_label: str
    origin: Literal["course", "external"] = "course"
    file_path: str  # relative to the workspace
    extra_files: list[str] = Field(default_factory=list)
    readiness: Literal["ready", "extracting", "failed", "ocr_unavailable"] = "ready"
    readiness_detail: str | None = None
    chars: int = 0
    excerpt: str = ""
    created_at: int = Field(default_factory=now_ms)


class Citation(BaseModel):
    source_id: str = ""
    source_name: str = ""
    location: str = ""
    excerpt: str = ""
    # The app's own check: source_id exists and the excerpt is found in that source's text. Not Drex's judgement.
    app_check: Literal["verified", "source_found_excerpt_missing", "unknown_source"] = "unknown_source"


class StudyTurn(BaseModel):
    id: str = Field(default_factory=lambda: new_id("turn"))
    speaker: str  # human | maya | leo | sam | priya | group (unattributed)
    message: str
    citations: list[Citation] = Field(default_factory=list)
    question_for_human: str | None = None
    next_action: str | None = None
    mode: str = "discussion"  # discussion | quiz | summary | research
    run_id: str | None = None
    created_at: int = Field(default_factory=now_ms)


class InternEvent(BaseModel):
    """One normalized event. `raw_payload` keeps the untouched Intern event (or raw text for a malformed line)."""

    id: str = Field(default_factory=lambda: new_id("ev"))
    kind: str  # system1_decision, tool_started, ... see normalizer.py
    layer: EventLayer
    severity: Severity = "info"
    title: str
    detail: str | None = None
    raw_type: str | None = None
    raw_payload: Any = None
    created_at: int = Field(default_factory=now_ms)


class StudyRoom(BaseModel):
    id: str
    title: str
    course: str = ""
    status: RoomStatus = "starting"
    status_detail: str | None = None
    created_at: int = Field(default_factory=now_ms)
    system1: str
    system2: str
    intern_version: str | None = None
    intern_args: list[str] = Field(default_factory=list)
    intern_pid: int | None = None
    protocol_version: int | None = None
    busy: bool = False
    current_mode: str = "discussion"
    participants: list[Participant] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    turns: list[StudyTurn] = Field(default_factory=list)
    drex_status: str = "Waiting for Intern System 1 events."
    latest_decision: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


def default_participants(human_name: str) -> list[Participant]:
    return [
        Participant(id="human", name=human_name or "You", role="Human student", kind="human",
                    initials=(human_name or "You")[:2].upper(), state="idle"),
        Participant(id="maya", name="Maya", role="Prepared explainer", kind="ai", initials="MA"),
        Participant(id="leo", name="Leo", role="Curious clarifier", kind="ai", initials="LE"),
        Participant(id="sam", name="Sam", role="Skeptical challenger", kind="ai", initials="SA"),
        Participant(id="priya", name="Priya", role="Problem solver", kind="ai", initials="PR"),
        Participant(id="observer", name="Nace Observer", role="Nace Drex Max v1.1 — System 1 Supervisor",
                    kind="observer", initials="N1", state="listening"),
    ]


# ---- request bodies ----

class CreateSessionBody(BaseModel):
    title: str = "Study Room"
    course: str = ""
    human_name: str = "You"


class TextSourceBody(BaseModel):
    name: str = "Pasted notes"
    kind: SourceKind = "notes"
    text: str


class YoutubeBody(BaseModel):
    url: str
    name: str = ""


class TurnBody(BaseModel):
    text: str


class QuizBody(BaseModel):
    topic: str = ""
    count: int = 3


class ResearchBody(BaseModel):
    query: str
