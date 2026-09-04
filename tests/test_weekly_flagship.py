"""Weekly flagship mode wiring and briefing title derivation."""

from types import SimpleNamespace

from bcn.briefing.quality import BriefingQualityGate
from bcn.briefing.text import derive_briefing_title
from bcn.common.config import Settings
from bcn.contracts.modes import ALL_WORKFLOW_MODES
from bcn.contracts.modes import WEEKLY_FLAGSHIP_MODE
from bcn.workflows.catalog import get_scheduled_workflow_definition
from bcn.workflows.catalog import iter_scheduled_workflows
from bcn.workflows.execution import _STEP_EXECUTORS


def test_weekly_mode_registered():
    assert WEEKLY_FLAGSHIP_MODE in ALL_WORKFLOW_MODES
    assert get_scheduled_workflow_definition(WEEKLY_FLAGSHIP_MODE) is not None
    enabled_ids = {
        definition.workflow_id
        for definition in iter_scheduled_workflows(
            Settings(weekly_flagship_enabled=True)
        )
    }
    assert WEEKLY_FLAGSHIP_MODE in enabled_ids
    assert ("workflow", "hold_flagship_for_review") in _STEP_EXECUTORS


def test_weekly_flagship_disabled_by_default():
    weekly = get_scheduled_workflow_definition(WEEKLY_FLAGSHIP_MODE)
    assert weekly is not None
    assert weekly.enabled_when is not None
    assert weekly.enabled_when(Settings()) is False
    assert weekly.enabled_when(Settings(weekly_flagship_enabled=True)) is True


def test_weekly_char_limits():
    gate = BriefingQualityGate(Settings())
    assert gate.char_limits("weekly_flagship") == (3000, 5000, 9000)


def test_derive_briefing_title_from_opener():
    markdown = (
        "The fun theme today: **trusted plumbing** accepting "
        "[attacker-shaped](https://example.com/x) nonsense, then acting surprised.\n\n"
        "**\U0001f9e8 Section One**\nBody text."
    )
    title = derive_briefing_title(markdown)
    assert "trusted plumbing" in title
    assert "[" not in title and "**" not in title
    assert len(title) <= 72


def test_derive_briefing_title_empty_markdown():
    assert derive_briefing_title("") == "Broken Cloud briefing"


def test_derive_briefing_title_skips_cover_image_line():
    markdown = (
        "![Daily Cover](https://cdn.example.com/cover.png)\n\n"
        "Four patches and a foothold on the endpoint agent.\n\n"
        "**Section**\nBody."
    )
    assert derive_briefing_title(markdown) == "Four patches and a foothold on the endpoint agent"


def test_briefing_claim_queries_select_title():
    """The title must travel from the claim query into the distributor payload."""
    import inspect

    from bcn.persistence import briefings

    for fn in (
        briefings.claim_latest_draft_briefing,
        briefings.claim_draft_briefing_by_id,
        briefings.get_briefing_by_id,
    ):
        assert "b.title" in inspect.getsource(fn), fn.__name__


def test_weekly_distribution_skips_stale_guard_and_uses_subscribers():
    import inspect

    from bcn.workflows import distribution

    source = inspect.getsource(distribution.execute_distribution)
    assert "WEEKLY_FLAGSHIP_MODE" in source
    recipients_source = inspect.getsource(distribution._newsletter_recipients_for_mode)
    assert "WEEKLY_FLAGSHIP_MODE" in recipients_source


def test_weekly_selection_mode_branch():
    from bcn.services.writer.selection import select_items_for_workflow

    items = [
        {
            "id": str(index),
            "title": f"Item {index}",
            "url": f"https://host{index}.example.com/a",
            "relevance_score": 9,
            "source_type": "rss",
        }
        for index in range(6)
    ]
    service = SimpleNamespace(
        settings=Settings(),
        selector=SimpleNamespace(
            is_duplicate_of=lambda item, selected: False,
            high_signal_count=lambda items_: len(items_),
            priority_score=lambda item, recent_published=None: 0.0,
        ),
    )
    result = select_items_for_workflow(service, items, WEEKLY_FLAGSHIP_MODE)
    assert result["decision"] == "generate"
    assert result["mode"] == "weekly_flagship"
    assert len(result["selected_items"]) >= 5
