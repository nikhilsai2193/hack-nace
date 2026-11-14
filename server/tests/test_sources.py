from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grounding  # noqa: E402
from adapters import youtube  # noqa: E402


def test_pptx_extraction(tmp_path: Path):
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "The 4 Ps"
    s1.placeholders[1].text = "Product, price, place, promotion"
    s1.notes_slide.notes_text_frame.text = "Stress consistency across the mix."
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "SWOT"
    table = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "Internal", "Strengths, weaknesses"
    table.cell(1, 0).text, table.cell(1, 1).text = "External", "Opportunities, threats"
    path = tmp_path / "deck.pptx"
    prs.save(path)

    text = grounding.extract_pptx(path)
    assert "[slide 1]" in text and "[slide 2]" in text
    assert "Product, price, place, promotion" in text
    assert "Speaker notes: Stress consistency across the mix." in text
    assert "External | Opportunities, threats" in text


def test_youtube_ids():
    vid = "8jPQjjsBbIc"
    for url in (f"https://www.youtube.com/watch?v={vid}&t=30s", f"https://youtu.be/{vid}",
                f"https://www.youtube.com/embed/{vid}", f"https://www.youtube.com/shorts/{vid}",
                f"https://m.youtube.com/watch?feature=share&v={vid}", vid):
        assert youtube.video_id(url) == vid
    assert youtube.video_id("https://example.com/watch") is None
