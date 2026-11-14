"""The final overview: a structured study guide System 2 writes as JSON, checked by the app, rendered for Adobe.

It replaces the old "paste the summary turns into a PDF" recap. Sections follow a revision-sheet layout: an
at-a-glance summary, key concepts with sources, an optional comparison table, the process step by step, exam focus,
common mistakes, what the sources do not cover, practice questions with a separate answer key, and next steps."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import grounding
from models import Citation, Source, StudyTurn

PROMPT = """{name} is ending the session and wants the FINAL OVERVIEW: a clean, printable study guide built from the \
course sources and everything the group established. This is a written document, not a conversation.

Write it as ONE JSON object and nothing else:
{{
  "overview": {{
    "title": "short topic title, e.g. 'Photosynthesis: light reactions and the Calvin cycle'",
    "tldr": "2-3 plain sentences: the big idea a student must walk away with",
    "key_concepts": [
      {{"term": "2-5 words", "explanation": "one clear sentence, max 25 words",
        "cite": {{"source_id": "S1", "location": "line/page/section", "excerpt": "verbatim words from the source"}}}}
    ],
    "comparison": {{"title": "X vs Y", "columns": ["", "X", "Y"],
                    "rows": [["Where", "...", "..."], ["Inputs", "...", "..."], ["Outputs", "...", "..."]]}},
    "process": {{"title": "How it fits together", "steps": ["one short sentence per step, in order"]}},
    "exam_focus": ["what the sources flag as most testable, one line each"],
    "misconceptions": [{{"wrong": "a common mistake", "right": "the correct idea", "cite": {{...same shape...}}}}],
    "gaps": ["something a student might expect that the sources do NOT cover, one line each"],
    "practice": [{{"question": "exam-style question", "answer": "short model answer", "cite": {{...}}}}],
    "next_steps": ["a concrete study action, starting with a verb"]
  }},
  "turns": [{{"speaker": "maya", "message": "One short spoken line telling {name} the study guide is ready.",
             "citations": []}}]
}}

Rules:
- Every fact comes from the course sources (priority: notes > slides/PDF > transcript > EXTERNAL). Anything not in
  the sources goes in "gaps", never in the other sections.
- 4-8 key_concepts, 3-6 process steps, 2-4 exam_focus, 2-4 misconceptions, 3-5 practice questions, exactly 3
  next_steps, 0-4 gaps. Use "comparison": null when nothing is naturally compared.
- Every "excerpt" is copied word for word from the source file (5-25 words). Never invent a citation.
- Plain text only inside strings: no markdown, no persona names, no chit-chat, no "we discussed". Write like a good
  textbook summary box: short, precise, scannable."""

TAIL = ("Reply with the single JSON object described above (overview + one spoken line), grounded in the sources. "
        "Excerpts must be verbatim from the source files.")

LIMITS = {"key_concepts": 8, "process_steps": 6, "exam_focus": 4, "misconceptions": 4, "gaps": 4, "practice": 5,
          "next_steps": 3}


def _find(text: str) -> dict[str, Any] | None:
    for cand in grounding._json_candidates(text or ""):  # noqa: SLF001 — same tolerant scanner as the turns
        if isinstance(cand, dict) and isinstance(cand.get("overview"), dict):
            return cand["overview"]
        if isinstance(cand, dict) and "key_concepts" in cand and "tldr" in cand:
            return cand
    return None


ROWS_FIX = re.compile(r'("columns"\s*:\s*\[[^\[\]]*\])\s*,\s*((?:\[[^\[\]]*\]\s*,?\s*)+)(?=\})')


def repair(text: str) -> str:
    """Fixes slips small models make in long JSON: a comparison table whose row arrays lost their "rows" key, and
    trailing commas before a closing bracket."""
    text = ROWS_FIX.sub(lambda m: f'{m.group(1)},"rows":[{m.group(2).rstrip().rstrip(",")}]', text)
    return re.sub(r",\s*([\]}])", r"\1", text)


def extract(text: str) -> dict[str, Any] | None:
    """The `overview` object from System 2's reply, tolerating code fences, surrounding prose and small JSON slips.
    As a last resort the optional comparison table is dropped so the rest of the guide survives."""
    found = _find(text) or _find(repair(text))
    if found is None and '"comparison"' in (text or ""):
        no_table = re.sub(r'"comparison"\s*:\s*\{.*?\}\s*,\s*(?="process")', "", repair(text), flags=re.S)
        found = _find(no_table)
    return found


def _s(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().strip("*_#`")


def _list(v: Any, n: int) -> list[Any]:
    return [x for x in v if x][:n] if isinstance(v, list) else []


def clean(ov: dict[str, Any]) -> dict[str, Any]:
    """Normalize shapes and cap lengths so a sloppy reply still renders cleanly."""
    comp = ov.get("comparison")
    if isinstance(comp, dict) and isinstance(comp.get("rows"), list) and comp["rows"]:
        cols = [_s(c) for c in comp.get("columns") or []]
        rows = [[_s(c) for c in r] for r in comp["rows"] if isinstance(r, list) and r][:8]
        width = max([len(cols)] + [len(r) for r in rows])
        cols = (cols + [""] * width)[:width]
        rows = [(r + [""] * width)[:width] for r in rows]
        comp = {"title": _s(comp.get("title")), "columns": cols, "rows": rows}
    else:
        comp = None
    proc = ov.get("process") if isinstance(ov.get("process"), dict) else {"steps": ov.get("process") or []}
    return {
        "title": _s(ov.get("title")),
        "tldr": _s(ov.get("tldr")),
        "key_concepts": [{"term": _s(k.get("term")), "explanation": _s(k.get("explanation")), "cite": k.get("cite")}
                         for k in _list(ov.get("key_concepts"), LIMITS["key_concepts"]) if isinstance(k, dict)],
        "comparison": comp,
        "process": {"title": _s(proc.get("title")) or "How it fits together",
                    "steps": [_s(x) for x in _list(proc.get("steps"), LIMITS["process_steps"])]},
        "exam_focus": [_s(x) for x in _list(ov.get("exam_focus"), LIMITS["exam_focus"])],
        "misconceptions": [{"wrong": _s(m.get("wrong")), "right": _s(m.get("right")), "cite": m.get("cite")}
                           for m in _list(ov.get("misconceptions"), LIMITS["misconceptions"]) if isinstance(m, dict)],
        "gaps": [_s(x) for x in _list(ov.get("gaps"), LIMITS["gaps"])],
        "practice": [{"question": _s(p.get("question")), "answer": _s(p.get("answer")), "cite": p.get("cite")}
                     for p in _list(ov.get("practice"), LIMITS["practice"]) if isinstance(p, dict)],
        "next_steps": [_s(x) for x in _list(ov.get("next_steps"), LIMITS["next_steps"])],
    }


def verify(ov: dict[str, Any], sources: list[Source], workspace: Path) -> dict[str, int]:
    """Runs the app's citation check on every `cite` and replaces it with the checked citation. Returns counts."""
    items = [x for key in ("key_concepts", "misconceptions", "practice") for x in ov[key]]
    cits = [Citation(**{k: str(v) for k, v in (x.get("cite") or {}).items()
                        if k in ("source_id", "source_name", "location", "excerpt")}) for x in items]
    probe = StudyTurn(speaker="maya", message="", citations=cits)
    grounding.verify_citations(probe, sources, workspace)
    for item, c in zip(items, probe.citations):
        item["cite"] = c.model_dump() if (c.source_id or c.excerpt) else None
    ok = sum(1 for c in probe.citations if c.app_check == "verified")
    return {"citations": len(cits), "verified": ok}


# ---------------------------------------------------------------- rendering

CSS = """
@page { size: letter; margin: 0.55in 0.6in; }
* { box-sizing: border-box; }
body { font-family: Helvetica, Arial, sans-serif; color: #1f2328; font-size: 10.5pt; line-height: 1.45; margin: 0; }
.head { border-bottom: 3px solid #1a73e8; padding-bottom: 10px; margin-bottom: 14px; }
.eyebrow { font-size: 8.5pt; letter-spacing: .08em; text-transform: uppercase; color: #1a73e8; font-weight: bold; }
h1 { font-size: 21pt; margin: 4px 0 4px; line-height: 1.15; }
.meta { color: #5f6368; font-size: 9pt; }
h2 { font-size: 12.5pt; margin: 18px 0 6px; color: #174ea6; page-break-after: avoid; }
h2 .n { display: inline-block; width: 20px; height: 20px; line-height: 20px; text-align: center; border-radius: 50%;
        background: #1a73e8; color: #fff; font-size: 9pt; margin-right: 6px; vertical-align: 2px; }
.tldr { background: #e8f0fe; border-left: 4px solid #1a73e8; padding: 10px 14px; border-radius: 4px; font-size: 11pt; }
table { width: 100%; border-collapse: collapse; margin: 4px 0; page-break-inside: avoid; }
th { background: #f1f3f4; text-align: left; font-size: 9pt; text-transform: uppercase; letter-spacing: .04em;
     color: #3c4043; padding: 6px 8px; border-bottom: 1px solid #dadce0; }
td { padding: 6px 8px; border-bottom: 1px solid #e8eaed; vertical-align: top; }
td.term { font-weight: bold; width: 26%; }
td.src { width: 16%; }
td.id { width: 8%; font-weight: bold; }
.cite { font-size: 8pt; color: #188038; white-space: nowrap; }
.cite.unv { color: #b06000; }
ol.steps { margin: 4px 0; padding-left: 0; list-style: none; counter-reset: s; }
ol.steps li { counter-increment: s; padding: 4px 0 4px 30px; position: relative; }
ol.steps li:before { content: counter(s); position: absolute; left: 0; top: 4px; width: 20px; height: 20px;
  line-height: 20px; text-align: center; border-radius: 4px; background: #e8f0fe; color: #174ea6; font-weight: bold;
  font-size: 9pt; }
.box { border: 1px solid #dadce0; border-radius: 6px; padding: 8px 12px; margin: 4px 0; page-break-inside: avoid; }
.focus { background: #fef7e0; border-color: #f9d36b; }
.gaps { background: #fce8e6; border-color: #f2b8b5; }
ul.tight { margin: 2px 0; padding-left: 18px; } ul.tight li { margin: 2px 0; }
.wrong { color: #a50e0e; } .right { color: #137333; }
.q { margin: 6px 0; } .qn { font-weight: bold; color: #174ea6; }
.key { font-size: 9.5pt; color: #3c4043; }
.sources td { font-size: 9pt; }
.foot { margin-top: 18px; border-top: 1px solid #e8eaed; padding-top: 6px; color: #80868b; font-size: 8pt; }
.page-break { page-break-before: always; }
"""


def _cite(c: dict[str, Any] | None) -> str:
    if not c or not (c.get("source_id") or c.get("excerpt")):
        return ""
    ok = c.get("app_check") == "verified"
    loc = f" · {html.escape(c['location'])}" if c.get("location") else ""
    mark = "&#10003;" if ok else "?"
    title = html.escape(c.get("excerpt") or "")
    return f'<span class="cite{"" if ok else " unv"}" title="{title}">{mark} {html.escape(c.get("source_id") or "?")}{loc}</span>'


def render_html(ov: dict[str, Any], meta: dict[str, Any]) -> str:
    e = html.escape
    n = 0
    parts: list[str] = []

    def section(title: str, body: str) -> None:
        nonlocal n
        n += 1
        parts.append(f'<h2><span class="n">{n}</span>{e(title)}</h2>{body}')

    if ov["key_concepts"]:
        rows = "".join(f'<tr><td class="term">{e(k["term"])}</td><td>{e(k["explanation"])}</td>'
                       f'<td class="src">{_cite(k.get("cite"))}</td></tr>' for k in ov["key_concepts"])
        section("Key concepts", f"<table><tr><th>Concept</th><th>What to know</th><th>Source</th></tr>{rows}</table>")
    comp = ov.get("comparison")
    if comp:
        head = "".join(f"<th>{e(c)}</th>" for c in comp["columns"])
        body = "".join("<tr>" + "".join(f'<td{" class=term" if i == 0 else ""}>{e(c)}</td>' for i, c in enumerate(r))
                       + "</tr>" for r in comp["rows"])
        section(comp["title"] or "At a glance: side by side", f"<table><tr>{head}</tr>{body}</table>")
    if ov["process"]["steps"]:
        steps = "".join(f"<li>{e(s)}</li>" for s in ov["process"]["steps"])
        section(ov["process"]["title"], f'<ol class="steps">{steps}</ol>')
    if ov["exam_focus"]:
        items = "".join(f"<li>{e(x)}</li>" for x in ov["exam_focus"])
        section("Exam focus", f'<div class="box focus"><ul class="tight">{items}</ul></div>')
    if ov["misconceptions"]:
        rows = "".join(f'<tr><td class="wrong">&#10007; {e(m["wrong"])}</td><td class="right">&#10003; {e(m["right"])}</td>'
                       f'<td class="src">{_cite(m.get("cite"))}</td></tr>' for m in ov["misconceptions"])
        section("Common mistakes", f"<table><tr><th>Mistake</th><th>Correct idea</th><th>Source</th></tr>{rows}</table>")
    if ov["gaps"]:
        items = "".join(f"<li>{e(x)}</li>" for x in ov["gaps"])
        section("Not covered by your sources",
                f'<div class="box gaps"><ul class="tight">{items}</ul>'
                '<div class="key">Check these in your textbook or with your instructor before relying on them.</div></div>')
    if ov["practice"]:
        qs = "".join(f'<div class="q"><span class="qn">Q{i}.</span> {e(p["question"])}</div>'
                     for i, p in enumerate(ov["practice"], 1))
        section("Practice questions", qs + '<div class="key">Answers are on the last page.</div>')
    if ov["next_steps"]:
        items = "".join(f"<li>{e(x)}</li>" for x in ov["next_steps"])
        section("Next steps", f'<ol class="steps">{items}</ol>')

    answer_key = ""
    if ov["practice"]:
        rows = "".join(f'<tr><td class="id">Q{i}</td><td>{e(p["answer"])}</td><td class="src">{_cite(p.get("cite"))}</td></tr>'
                       for i, p in enumerate(ov["practice"], 1))
        answer_key = (f'<div class="page-break"></div><h2>Answer key</h2>'
                      f"<table><tr><th>Q</th><th>Answer</th><th>Source</th></tr>{rows}</table>")
    src_rows = "".join(f'<tr><td class="id">{e(s["id"])}</td><td>{e(s["name"])}</td><td>{e(s["priority"])}</td></tr>'
                       for s in meta["sources"])
    stats = meta.get("stats") or {}
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(ov['title'] or meta['title'])}</title>
<style>{CSS}</style></head><body>
<div class="head"><div class="eyebrow">Study guide · {e(meta['course'] or meta['title'])}</div>
<h1>{e(ov['title'] or meta['title'])}</h1>
<div class="meta">{e(meta['generated_at'])} · {len(meta['sources'])} source(s) · {stats.get('verified', 0)}/{stats.get('citations', 0)} citations verified against the source text</div></div>
<div class="tldr"><b>In short:</b> {e(ov['tldr'])}</div>
{''.join(parts)}
{answer_key}
<h2>Sources</h2><table class="sources">{src_rows}</table>
<div class="foot">&#10003; = the quoted excerpt was found word for word in the source (the app's own check). ? = not found.
Written by System 2 (DeepInfra) and supervised by Nace Drex Max v1.1 (System 1) through Intern.</div>
</body></html>"""
