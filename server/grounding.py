"""Course sources on disk, the manifest Intern reads, tolerant parsing of System 2's output, and the app's own
citation check (source id exists and the excerpt is found in the source text). This check is the app's, not Drex's."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from models import SOURCE_PRIORITY, Citation, Source, StudyTurn

SPEAKERS = {"maya", "leo", "sam", "priya"}
TEXT_EXT = {".txt", ".md", ".markdown"}
IMAGE_EXT = {".png", ".jpg", ".jpeg"}


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return s[:60] or "source"


def make_source(sid: str, name: str, kind: str, rel_path: str, text: str, origin: str = "course") -> Source:
    prio, label = SOURCE_PRIORITY.get(kind, SOURCE_PRIORITY["text"])
    return Source(id=sid, name=name, kind=kind, priority=prio, priority_label=label, origin=origin,  # type: ignore[arg-type]
                  file_path=rel_path, chars=len(text), excerpt=text[:400])


def extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        parts.append(f"\n\n[page {i}]\n" + (page.extract_text() or ""))
    return "".join(parts).strip()


def extract_pptx(path: Path) -> str:
    """Slide text (titles, text boxes, tables) plus speaker notes, one block per slide."""
    from pptx import Presentation

    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if t.strip():
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    texts.append(" | ".join(c.text for c in row.cells))
        notes = ""
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        block = f"[slide {i}]\n" + "\n".join(texts)
        if notes:
            block += f"\nSpeaker notes: {notes}"
        parts.append(block)
    return "\n\n".join(parts).strip()


OCR_SCRIPT = """
import sys
from rapidocr_onnxruntime import RapidOCR
result, _ = RapidOCR()(sys.argv[1])
print("\\n".join(r[1] for r in (result or [])))
"""


def ocr_image(python: str, path: Path) -> str:
    """OCR with the rapidocr engine that Intern's [ocr] extra installs, run in Intern's own interpreter."""
    r = subprocess.run([python, "-c", OCR_SCRIPT, str(path)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "OCR failed").strip()[-500:])
    return r.stdout.strip()


def write_manifest(workspace: Path, sources: list[Source]) -> None:
    lines = [
        "# Course sources manifest",
        "",
        "Priority: P1 handwritten notes/notes > P2 slides/PDFs > P3 lecture transcript > P4 workspace grounding > "
        "P5 EXTERNAL research. External research never overrides course material.",
        "",
        "| id | name | type | priority | origin | file (relative to workspace) | readiness |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in sorted(sources, key=lambda s: (s.priority, s.id)):
        files = ", ".join([s.file_path, *s.extra_files])
        lines.append(f"| {s.id} | {s.name} | {s.kind} | {s.priority_label} | {s.origin.upper()} | {files} | "
                     f"{s.readiness} |")
    (workspace / "sources" / "manifest.md").write_text("\n".join(lines) + "\n")


def source_text(workspace: Path, s: Source) -> str:
    try:
        return (workspace / s.file_path).read_text(errors="ignore")
    except OSError:
        return ""


# ---------- System 2 output parsing ----------

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _json_candidates(text: str) -> list[Any]:
    found: list[Any] = []
    blocks = FENCE.findall(text) or [text]
    for block in blocks:
        block = block.strip()
        try:
            found.append(json.loads(block))
            continue
        except json.JSONDecodeError:
            pass
        # Scan for top-level JSON values embedded in prose.
        dec = json.JSONDecoder()
        i = 0
        while i < len(block):
            if block[i] in "[{":
                try:
                    val, end = dec.raw_decode(block, i)
                    found.append(val)
                    i = end
                    continue
                except json.JSONDecodeError:
                    pass
            i += 1
    return found


def _flatten(val: Any) -> list[dict[str, Any]]:
    if isinstance(val, list):
        return [x for v in val for x in _flatten(v)]
    if isinstance(val, dict):
        if isinstance(val.get("turns"), list):
            return _flatten(val["turns"])
        if "speaker" in val and "message" in val:
            return [val]
    return []


def parse_system2(text: str, mode: str, run_id: str | None) -> list[StudyTurn]:
    text = (text or "").strip()
    if not text:
        return []
    objs = [o for c in _json_candidates(text) for o in _flatten(c)]
    if not objs:
        # No JSON: keep the plain text as one unattributed turn. Never fail the app over formatting.
        return [StudyTurn(speaker="group", message=text, mode=mode, run_id=run_id)]
    turns = []
    for o in objs:
        speaker = str(o.get("speaker", "group")).strip().lower()
        if speaker not in SPEAKERS:
            speaker = "group"
        cits = []
        for c in o.get("citations") or []:
            if isinstance(c, dict):
                cits.append(Citation(source_id=str(c.get("source_id", "")), source_name=str(c.get("source_name", "")),
                                     location=str(c.get("location", "")), excerpt=str(c.get("excerpt", ""))))
        q = o.get("question_for_human")
        turns.append(StudyTurn(speaker=speaker, message=str(o.get("message", "")), citations=cits,
                               question_for_human=str(q) if q else None,
                               next_action=str(o.get("next_action")) if o.get("next_action") else None,
                               mode=mode, run_id=run_id))
    return turns


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[“”\"'‘’`*_]", "", s)).strip().lower()


def verify_citations(turn: StudyTurn, sources: list[Source], workspace: Path) -> None:
    by_id = {s.id.lower(): s for s in sources}
    by_name = {s.name.lower(): s for s in sources}
    cache: dict[str, str] = {}
    for c in turn.citations:
        s = by_id.get(c.source_id.strip().lower()) or by_name.get(c.source_name.strip().lower())
        if not s:
            c.app_check = "unknown_source"
            continue
        c.source_id, c.source_name = s.id, s.name
        if s.id not in cache:
            cache[s.id] = _norm(source_text(workspace, s))
        raw = c.excerpt.strip(" .…").replace("...", "\x00").replace("…", "\x00")
        pieces = [_norm(p) for p in raw.split("\x00") if _norm(p)]
        ex = " ".join(pieces)
        ok = bool(ex) and len(ex) >= 8 and all(p in cache[s.id] for p in pieces)
        c.app_check = "verified" if ok else "source_found_excerpt_missing"
