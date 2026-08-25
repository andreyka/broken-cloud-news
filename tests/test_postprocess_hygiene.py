"""Regression tests for enforce_release_link_hygiene ordering guarantees."""

from bcn.common.config import Settings
from bcn.services.writer.postprocess import WriterPostprocessor


def _postprocessor() -> WriterPostprocessor:
    return WriterPostprocessor(
        Settings(),
        writer_llm=None,
        priority_score=lambda item: 0.0,
        char_limits=lambda mode, selected_count=None: (1200, 1700, 2300),
    )


def _item(idx: int) -> dict:
    return {
        "title": f"Item {idx}",
        "url": f"https://example.com/item-{idx}",
    }


def _covered_paragraph(idx: int, pad: int = 300) -> str:
    filler = f"word{idx} " * (pad // 7)
    return f"**Section {idx}**\n{filler}[Item {idx}](https://example.com/item-{idx})."


def test_overlength_body_keeps_all_urls_within_cap() -> None:
    """Clipping for length must not silently drop tail URLs (they get re-appended)."""
    items = [_item(i) for i in range(1, 7)]
    body = "\n\n".join(_covered_paragraph(i, pad=400) for i in range(1, 7))
    assert len(body) > 2300

    result = _postprocessor().enforce_release_link_hygiene(
        body, items, hard_max_chars=2300
    )

    assert len(result) <= 2300
    for i in range(1, 7):
        assert f"https://example.com/item-{i}" in result


def test_missing_item_references_survive_and_fit() -> None:
    """Appended references must fit inside hard_max_chars, never be clipped off."""
    items = [_item(i) for i in range(1, 6)]
    covered = "\n\n".join(_covered_paragraph(i, pad=560) for i in range(1, 5))
    assert len(covered) > 1800  # missing item 5 forces an append near the cap

    result = _postprocessor().enforce_release_link_hygiene(
        covered, items, hard_max_chars=2300
    )

    assert len(result) <= 2300
    for i in range(1, 6):
        assert f"https://example.com/item-{i}" in result


def test_clean_short_body_passes_through() -> None:
    items = [_item(1)]
    body = _covered_paragraph(1, pad=200)

    result = _postprocessor().enforce_release_link_hygiene(
        body, items, hard_max_chars=2300
    )

    assert "https://example.com/item-1" in result
    assert len(result) <= 2300
