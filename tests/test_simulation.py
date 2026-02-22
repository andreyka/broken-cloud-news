from __future__ import annotations

from bcn.simulation import score_feedback_rubric


def test_score_feedback_rubric_penalizes_monoculture_and_thin_content():
    markdown = "Cloud update."
    items = [
        {
            "source_type": "rss",
            "url": "https://unit42.paloaltonetworks.com/post-a",
            "title": "General AI trend",
            "summary": "No patch details",
        },
        {
            "source_type": "rss",
            "url": "https://unit42.paloaltonetworks.com/post-b",
            "title": "Another trend",
            "summary": "No operator moves",
        },
    ]
    gate = {"hard_issues": [], "soft_issues": []}

    scored = score_feedback_rubric(
        markdown,
        items,
        gate,
        min_chars=300,
        hard_max_chars=1800,
    )

    assert scored["score"] < 60
    notes = " ".join(scored["notes"])
    assert "No Reddit-sourced item" in notes
    assert "No Cloudflare-side signal" in notes


def test_score_feedback_rubric_rewards_actionable_diverse_cloud_content():
    markdown = (
        "**Control Plane Heat**\n"
        "[Defending QUIC](https://blog.cloudflare.com/defending-quic-from-acknowledgement-based-ddos-attacks/) "
        "patches CVE-2025-4820; enforce ACK validation and roll detection rules.\n\n"
        "**Operator Moves (next 24h)**\n"
        "- Patch vulnerable envoy and kubernetes ingress builds.\n"
        "- Hunt IOC patterns for exploit traffic in load balancer logs.\n"
        "- Validate redis and postgres hardening baselines."
    )
    items = [
        {
            "source_type": "rss",
            "url": "https://blog.cloudflare.com/defending-quic-from-acknowledgement-based-ddos-attacks/",
            "title": "Cloudflare QUIC defense",
            "summary": "CVE patch and mitigation details",
        },
        {
            "source_type": "reddit",
            "url": "https://www.reddit.com/r/netsec/comments/abc123/quic_writeup/",
            "title": "Netsec write-up",
            "summary": "Operator detection tips",
        },
    ]
    gate = {"hard_issues": [], "soft_issues": []}

    scored = score_feedback_rubric(
        markdown,
        items,
        gate,
        min_chars=200,
        hard_max_chars=2300,
    )

    assert scored["score"] >= 75
    assert scored["has_reddit"] is True
    assert scored["has_cloudflare"] is True
