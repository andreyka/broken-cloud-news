from bcn.history import extract_unique_post_urls
from bcn.history import parse_channel_history_text


def test_parse_channel_history_text_parses_multiple_posts() -> None:
    raw = (
        "Broken Cloud, [2/16/2026 6:57 AM]\n"
        "CSP Risk Reality Check\n"
        "\n"
        "Broken Cloud, [2/16/2026 9:03 AM]\n"
        "Agent Wars\n"
        "https://x.com/tom_doerr/status/2023303284367184074\n"
    )

    posts = parse_channel_history_text(raw, timezone_name="America/Los_Angeles")
    assert len(posts) == 2
    assert posts[0].author == "Broken Cloud"
    assert posts[0].content_markdown == "CSP Risk Reality Check"
    assert posts[0].posted_at.isoformat() == "2026-02-16T06:57:00-08:00"
    assert posts[1].content_markdown.startswith("Agent Wars")


def test_parse_channel_history_text_skips_empty_posts() -> None:
    raw = (
        "Broken Cloud, [3/1/2026 12:41 PM]\n"
        "\n"
        "\n"
        "Broken Cloud, [3/1/2026 12:42 PM]\n"
        "Non-empty post body\n"
    )

    posts = parse_channel_history_text(raw, timezone_name="America/Los_Angeles")
    assert len(posts) == 1
    assert posts[0].content_markdown == "Non-empty post body"


def test_extract_unique_post_urls_dedupes_by_canonical_key() -> None:
    content = (
        "One https://www.example.com/path?utm_source=x&utm_medium=y\n"
        "Two https://example.com/path\n"
        "Three https://example.com/path?b=2&a=1\n"
        "Four https://example.com/path?a=1&b=2\n"
    )
    urls = extract_unique_post_urls(content)
    assert urls == [
        "https://example.com/path",
        "https://example.com/path?a=1&b=2",
    ]
