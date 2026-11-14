"""Unit tests for the app's own logic: System 2 output parsing, the app-side citation check, and event labelling.
Event payloads follow the shapes in Intern 0.30.1's `intern schema`. No Intern process or model is involved."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grounding  # noqa: E402
import normalizer  # noqa: E402


def test_parse_array_in_fence():
    text = '```json\n[{"speaker":"maya","message":"Hi","citations":[],"next_action":"continue"},' \
           '{"speaker":"Sam","message":"Check","citations":[]}]\n```'
    turns = grounding.parse_system2(text, "discussion", None)
    assert [t.speaker for t in turns] == ["maya", "sam"]


def test_parse_turns_wrapper_and_prose():
    text = 'Here you go: {"turns":[{"speaker":"priya","message":"Q1","question_for_human":"Try it?"}]} thanks'
    turns = grounding.parse_system2(text, "quiz", None)
    assert turns[0].speaker == "priya" and turns[0].question_for_human == "Try it?"


def test_parse_plain_text_is_unattributed():
    turns = grounding.parse_system2("Just some prose.", "discussion", None)
    assert len(turns) == 1 and turns[0].speaker == "group"


def test_citation_check(tmp_path: Path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "S1.md").write_text("# Notes\n\nThe mitochondria is the powerhouse of the cell.\n")
    src = grounding.make_source("S1", "Bio notes", "notes", "sources/S1.md", "x")
    text = ('[{"speaker":"maya","message":"m","citations":['
            '{"source_id":"S1","excerpt":"the  powerhouse of the cell"},'
            '{"source_id":"S1","excerpt":"ribosomes make proteins"},'
            '{"source_id":"S9","excerpt":"anything"}]}]')
    turn = grounding.parse_system2(text, "discussion", None)[0]
    grounding.verify_citations(turn, [src], tmp_path)
    assert [c.app_check for c in turn.citations] == ["verified", "source_found_excerpt_missing", "unknown_source"]


def test_supervise_decision_warning():
    raw = {"type": "decision", "purpose": "supervise", "backend": "console", "questions": 1, "ok": True,
           "cached": False, "cost_usd": 0.0, "duration_ms": 10, "turn": 3,
           "answers": {"mode": {"choice": "retrying", "probabilities": {"retrying": 0.6, "re_investigating": 0.3,
                                                                          "advancing": 0.1}}}}
    evs = normalizer.normalize(raw)
    assert evs[0].title.startswith("Watching turn 3: retrying")
    assert evs[1].kind == "system1_warning"
    assert evs[0].raw_payload is raw


def test_guard_layers_labelled_separately():
    base = {"type": "tool_guard", "turn": 1, "call_id": "c", "name": "bash", "rule": "r", "reason": "why"}
    assert normalizer.normalize({**base, "layer": "system1"})[0].title.startswith("Tool call blocked by Drex")
    assert normalizer.normalize({**base, "layer": "rule"})[0].title.startswith("Blocked by guard rule (code)")


def test_nudges_and_run_end():
    assert "Loop detected" in normalizer.normalize({"type": "nudge", "turn": 2, "reason": "stuck_gate", "text": "x"})[0].title
    assert "Harness nudge" in normalizer.normalize({"type": "nudge", "turn": 2, "reason": "cut_off", "text": "x"})[0].title
    blocked = normalizer.normalize({"type": "run_end", "run_id": "r", "reason": "system1_unavailable", "turns": 1,
                                    "usage": {}, "final_text": "", "duration_ms": 1})[0]
    assert "BLOCKED" in blocked.title and blocked.severity == "error"


def test_unsupported_protocol_flagged():
    evs = normalizer.normalize({"type": "run_start", "protocol_version": 3, "session_id": "s", "run_id": "r",
                                "model": "m", "cwd": "/", "window": {}, "tools": []})
    assert any(e.kind == "session_error" for e in evs)
