from __future__ import annotations

from bcn.briefing.story_identity import primary_story_issue_key


def test_primary_story_issue_key_uses_explicit_advisory_ids_only():
    assert (
        primary_story_issue_key(
            "Flowise NVIDIA endpoint auth bypass advisory",
            "GHSA-5f53-522j-j454 allows unauthenticated access.",
        )
        == "ghsa-5f53-522j-j454"
    )


def test_primary_story_issue_key_ignores_generic_topic_signature():
    assert (
        primary_story_issue_key(
            "Flowise adds new auth hardening around model endpoints",
            "Vendor post about endpoint security posture and middleware behavior.",
        )
        == ""
    )
