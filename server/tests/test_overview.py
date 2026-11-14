from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grounding  # noqa: E402
import overview  # noqa: E402

REPLY = {
    "overview": {
        "title": "Photosynthesis",
        "tldr": "Light reactions make ATP and NADPH; the Calvin cycle spends them to fix CO2.",
        "key_concepts": [
            {"term": "**Light reactions**", "explanation": "Make ATP and NADPH in the thylakoids.",
             "cite": {"source_id": "S1", "location": "line 2", "excerpt": "produce ATP and NADPH"}},
            {"term": "RuBisCO", "explanation": "Starts carbon fixation.",
             "cite": {"source_id": "S1", "location": "line 9", "excerpt": "an invented quote that is not there"}},
        ],
        "comparison": {"title": "Light vs Calvin", "columns": ["", "Light", "Calvin"],
                       "rows": [["Where", "Thylakoid", "Stroma"], ["Output"]]},
        "process": {"steps": ["Light is absorbed", "Water is split"]},
        "exam_focus": ["Which stage makes O2"],
        "misconceptions": [{"wrong": "O2 comes from CO2", "right": "O2 comes from water"}],
        "gaps": ["Photosystems"],
        "practice": [{"question": "Where is O2 made?", "answer": "Light reactions"}],
        "next_steps": ["Draw the cycle", "Quiz yourself", "Re-read S1", "extra step dropped"],
    },
    "turns": [{"speaker": "maya", "message": "The guide is ready.", "citations": []}],
}


def test_extract_clean_verify_render(tmp_path: Path):
    text = "```json\n" + json.dumps(REPLY) + "\n```"
    ov = overview.clean(overview.extract(text))
    assert ov["key_concepts"][0]["term"] == "Light reactions"  # markdown markers stripped
    assert ov["comparison"]["rows"][1] == ["Output", "", ""]  # ragged rows padded
    assert len(ov["next_steps"]) == 3  # capped
    # the spoken line still parses as a turn
    assert [t.speaker for t in grounding.parse_system2(text, "overview", None)] == ["maya"]

    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "S1.md").write_text("The light reactions produce ATP and NADPH.\n")
    src = grounding.make_source("S1", "Notes", "notes", "sources/S1.md", "x")
    stats = overview.verify(ov, [src], tmp_path)
    assert stats == {"citations": 4, "verified": 1}
    assert ov["key_concepts"][0]["cite"]["app_check"] == "verified"
    assert ov["key_concepts"][1]["cite"]["app_check"] == "source_found_excerpt_missing"

    page = overview.render_html(ov, {"title": "T", "course": "BIO", "generated_at": "today", "stats": stats,
                                     "sources": [{"id": "S1", "name": "Notes", "priority": "P1"}]})
    for heading in ("Key concepts", "Light vs Calvin", "Exam focus", "Common mistakes", "Not covered by your sources",
                    "Practice questions", "Answer key", "Next steps", "1/4 citations verified"):
        assert heading in page
    assert "**" not in page


def test_extract_none_for_plain_turns():
    assert overview.extract('[{"speaker":"maya","message":"hi"}]') is None


def test_repairs_missing_rows_key_from_real_reply():
    # The exact slip Qwen3.8-27B made in a live run: the comparison's row arrays lost their "rows" key.
    broken = ('{"overview":{"title":"T","tldr":"x","key_concepts":[],"comparison":{"title":"Segmentation vs Targeting",'
              '"columns":["","Segmentation","Targeting"],["What it does","Divides","Chooses"],["Output","Groups","Pick"]},'
              '"process":{"steps":["a"]},"next_steps":["b",]},"turns":[]}')
    ov = overview.clean(overview.extract(broken))
    assert ov["comparison"]["rows"] == [["What it does", "Divides", "Chooses"], ["Output", "Groups", "Pick"]]
    assert ov["next_steps"] == ["b"]
