"""GHSA collection adapter for the collector service."""

from __future__ import annotations

import logging
import re
from typing import Any

from bcn.common.models import CollectedNewsItem
from bcn.services.collector.common import SKIP_SCRAPE_DOMAINS
from bcn.services.collector.common import coerce_feed_datetime
from bcn.services.collector.common import validate_source_timestamp

logger = logging.getLogger(__name__)

GHSA_QUERY = """
query {
  securityAdvisories(first: 100, orderBy: {field: PUBLISHED_AT, direction: DESC}) {
    nodes {
      ghsaId
      summary
      description
      permalink
      severity
      publishedAt
      references { url }
      identifiers { type value }
    }
  }
}
"""


async def collect_ghsa_items(service: Any) -> list[CollectedNewsItem]:
    """Fetch GitHub Security Advisories matching cloud keywords."""
    if not service.settings.github_token:
        logger.warning("No GitHub token configured, skipping GHSA collection")
        return []

    response = await service._http.post(
        "https://api.github.com/graphql",
        headers={
            "Authorization": f"Bearer {service.settings.github_token}",
            "User-Agent": "bcn-cloud-agent",
            "Content-Type": "application/json",
        },
        json={"query": GHSA_QUERY},
    )
    response.raise_for_status()
    data = response.json()

    nodes: list[dict[str, Any]] = (
        data.get("data", {}).get("securityAdvisories", {}).get("nodes", [])
    )
    keyword_patterns = [
        re.compile(keyword, re.IGNORECASE) for keyword in service.settings.ghsa_keywords
    ]
    allowed = set(service.settings.ghsa_severities)

    items: list[CollectedNewsItem] = []
    for item in nodes:
        if item.get("severity") not in allowed:
            continue

        text = f"{item.get('summary', '')} {item.get('description', '')}"
        if not any(pattern.search(text) for pattern in keyword_patterns):
            continue

        references = [ref["url"] for ref in item.get("references", [])]
        url = next(
            (
                candidate
                for candidate in references
                if "github.com" not in candidate and "nist.gov" not in candidate
            ),
            item.get("permalink", ""),
        )
        published_at = validate_source_timestamp(
            coerce_feed_datetime(item.get("publishedAt")),
            source_type="ghsa",
            source_id=str(item.get("ghsaId") or ""),
            title=str(item.get("summary") or ""),
            url=str(url or ""),
            field="publishedAt",
        )
        if published_at is None:
            continue
        full_content = await enrich_ghsa_content(service, item, references)
        items.append(
            CollectedNewsItem(
                source_type="ghsa",
                source_id=item["ghsaId"],
                url=url,
                title=item.get("summary"),
                published_at=published_at,
                raw_data=item,
                full_content=full_content or None,
            )
        )

    return items


async def enrich_ghsa_content(
    service: Any,
    item: dict[str, Any],
    references: list[str],
) -> str:
    """Build enriched content for a GHSA item by scraping reference links."""
    parts: list[str] = []

    description = item.get("description", "")
    if description:
        parts.append(f"[Advisory Description]\n{description}")

    cves = [
        ident["value"]
        for ident in item.get("identifiers", [])
        if ident.get("type") == "CVE"
    ]
    if cves:
        parts.append(f"[CVE IDs] {', '.join(cves)}")

    parts.append(f"[Severity] {item.get('severity', 'UNKNOWN')}")

    github_refs: list[str] = []
    other_refs: list[str] = []
    for ref_url in references:
        if any(domain in ref_url for domain in SKIP_SCRAPE_DOMAINS):
            continue
        if "github.com" in ref_url:
            github_refs.append(ref_url)
        else:
            other_refs.append(ref_url)

    scrape_targets = github_refs[:2] + other_refs[:1]
    for ref_url in scrape_targets:
        try:
            scraped = await service.scraper.scrape(ref_url)
            if scraped and len(scraped) >= service.scraper.min_content_length:
                label = "GitHub" if "github.com" in ref_url else "Blog/Write-up"
                parts.append(f"[{label}: {ref_url}]\n{scraped[:3000]}")
                logger.info(
                    "GHSA enrichment: scraped %s (%d chars)", ref_url, len(scraped)
                )
        except Exception as exc:
            logger.warning("GHSA enrichment: failed to scrape %s: %s", ref_url, exc)

    return "\n\n---\n\n".join(parts) if parts else ""


__all__ = [
    "GHSA_QUERY",
    "collect_ghsa_items",
    "enrich_ghsa_content",
]
