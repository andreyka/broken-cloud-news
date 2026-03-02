"""Collector agent: fetches news from GHSA, RSS feeds, Reddit, and Twitter/X."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message
import feedparser
import httpx
from typing_extensions import override

from bcn.agents.base import enqueue_event_safe
from bcn.common.config import Settings
from bcn.common.db import insert_news_item
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="collect_ghsa",
        name="Collect GHSA",
        description="Collect GitHub Security Advisories (CRITICAL/HIGH, cloud-related)",
        tags=["ghsa", "github"],
        examples=["collect ghsa"],
    ),
    AgentSkill(
        id="collect_rss",
        name="Collect RSS",
        description="Collect from CISA and AWS Security Blog RSS feeds",
        tags=["rss", "cisa", "aws"],
        examples=["collect rss"],
    ),
    AgentSkill(
        id="collect_twitter",
        name="Collect Twitter",
        description="Collect tweets from security researchers via X API",
        tags=["twitter", "x"],
        examples=["collect twitter"],
    ),
    AgentSkill(
        id="collect_reddit",
        name="Collect Reddit",
        description="Collect top items from cloud security subreddits via RSS",
        tags=["reddit", "netsec", "subreddit"],
        examples=["collect reddit"],
    ),
    AgentSkill(
        id="collect_all",
        name="Collect All",
        description="Run all collectors concurrently",
        tags=["all"],
        examples=["collect all", "collect"],
    ),
]

# GHSA GraphQL query
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

_SKIP_SCRAPE_DOMAINS = frozenset(
    {
        "nvd.nist.gov",
        "cve.mitre.org",
        "cve.org",
        "access.redhat.com/errata",
    }
)
_REDDIT_LOW_SIGNAL_DOMAINS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "facebook.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "imgur.com",
        "giphy.com",
    }
)
_REDDIT_TECHNICAL_DOMAIN_HINTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "cisa.gov",
        "nist.gov",
        "mitre.org",
        "aws.amazon.com",
        "cloud.google.com",
        "security.googleblog.com",
        "unit42.paloaltonetworks.com",
        "research.checkpoint.com",
        "stepsecurity.io",
    }
)
_REDDIT_TECHNICAL_HINTS = (
    "advisory",
    "cve",
    "vuln",
    "exploit",
    "rce",
    "xss",
    "ssrf",
    "auth",
    "bypass",
    "privilege",
    "escape",
    "container",
    "kubernetes",
    "k8s",
    "cloud",
    "iam",
    "s3",
    "supply chain",
    "supply-chain",
    "zero-day",
    "zeroday",
    "writeup",
    "write-up",
    "research",
    "patch",
    "poc",
    "proof of concept",
    "github actions",
)


class CollectorExecutor(AgentExecutor):
    """A2A agent that collects cloud security news from multiple sources."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scraper = Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )
        self._http = httpx.AsyncClient(timeout=60)
        self._last_collect_failures: dict[str, str] = {}

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Dispatch to the appropriate collector based on the user message."""
        msg = context.get_user_input() or "collect_all"
        text = msg.lower()

        if "ghsa" in text:
            count = await self._collect_ghsa()
            result = f"GHSA: collected {count} items"
        elif "rss" in text:
            count = await self._collect_rss()
            result = f"RSS: collected {count} items"
        elif "twitter" in text:
            count = await self._collect_twitter()
            result = f"Twitter: collected {count} items"
        elif "reddit" in text:
            count = await self._collect_reddit()
            result = f"Reddit: collected {count} items"
        else:
            counts = await self._collect_all()
            result = (
                f"All: GHSA={counts[0]}, RSS={counts[1]}, "
                f"Twitter={counts[2]}, Reddit={counts[3]}"
            )
            if self._last_collect_failures:
                failed = ", ".join(sorted(self._last_collect_failures))
                result += f" (failures: {failed})"

        logger.info(result)
        await enqueue_event_safe(event_queue, new_agent_text_message(result))

    async def close(self) -> None:
        """Release collector resources."""
        await self.scraper.close()
        await self._http.aclose()

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")

    async def _collect_all(self) -> tuple[int, int, int, int]:
        """Run all collectors concurrently and return per-source counts."""
        sources: list[tuple[str, Any]] = [
            ("ghsa", self._collect_ghsa()),
            ("rss", self._collect_rss()),
            ("twitter", self._collect_twitter()),
            ("reddit", self._collect_reddit()),
        ]
        results = await asyncio.gather(
            *(coro for _name, coro in sources),
            return_exceptions=True,
        )
        failures: dict[str, str] = {}
        counts: list[int] = []
        for (source, _coro), result in zip(sources, results):
            if isinstance(result, int):
                counts.append(result)
                continue

            counts.append(0)
            failures[source] = f"{result.__class__.__name__}: {result}"
            logger.error(
                "Collector source %s failed",
                source,
                exc_info=(result.__class__, result, result.__traceback__),
            )

        self._last_collect_failures = failures
        return tuple(counts)

    def _is_cloud_security_relevant(self, text: str) -> bool:
        """Heuristic topical filter to keep cloud-security signal high."""
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized or len(normalized) < 20:
            return False

        required = self.settings.twitter_required_keywords
        if required and not any(kw.lower() in normalized for kw in required):
            return False

        # Drop mostly social/noise posts from handle streams.
        if "ctf" in normalized and not any(
            t in normalized for t in ("cloud", "k8s", "container", "cve", "vuln")
        ):
            return False
        if normalized.startswith("rt @"):
            return False

        return True

    @staticmethod
    def _clean_summary(value: str) -> str:
        """Strip basic HTML tags/entities from feed summaries."""
        if not value:
            return ""
        text = re.sub(r"<[^>]+>", " ", value)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------
    # GHSA collection
    # ------------------------------------------------------------------

    async def _collect_ghsa(self) -> int:
        """Fetch GitHub Security Advisories matching cloud keywords.

        Returns:
            Number of newly inserted items.
        """
        if not self.settings.github_token:
            logger.warning("No GitHub token configured, skipping GHSA collection")
            return 0

        resp = await self._http.post(
            "https://api.github.com/graphql",
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
                "User-Agent": "bcn-cloud-agent",
                "Content-Type": "application/json",
            },
            json={"query": GHSA_QUERY},
        )
        resp.raise_for_status()
        data = resp.json()

        nodes: list[dict] = (
            data.get("data", {}).get("securityAdvisories", {}).get("nodes", [])
        )

        keyword_patterns = [
            re.compile(kw, re.IGNORECASE) for kw in self.settings.ghsa_keywords
        ]
        allowed = set(self.settings.ghsa_severities)

        count = 0
        for item in nodes:
            if item.get("severity") not in allowed:
                continue

            text = f"{item.get('summary', '')} {item.get('description', '')}"
            if not any(p.search(text) for p in keyword_patterns):
                continue

            refs = [r["url"] for r in item.get("references", [])]

            url = next(
                (u for u in refs if "github.com" not in u and "nist.gov" not in u),
                item.get("permalink", ""),
            )

            full_content = await self._enrich_ghsa_content(item, refs)

            inserted = await insert_news_item(
                source_type="ghsa",
                source_id=item["ghsaId"],
                url=url,
                title=item.get("summary"),
                published_at=item.get(
                    "publishedAt", datetime.now(timezone.utc).isoformat()
                ),
                raw_data=item,
                full_content=full_content or None,
            )
            if inserted:
                count += 1

        return count

    async def _enrich_ghsa_content(
        self,
        item: dict,
        refs: list[str],
    ) -> str:
        """Build enriched content for a GHSA item by scraping reference links.

        Prioritizes blog posts, GitHub repo descriptions, and PoC repos.
        Falls back to the advisory description if scraping yields nothing.

        Args:
            item: The raw GHSA advisory node.
            refs: Reference URLs from the advisory.

        Returns:
            Concatenated content sections separated by ``---``.
        """
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
        for ref_url in refs:
            if any(d in ref_url for d in _SKIP_SCRAPE_DOMAINS):
                continue
            if "github.com" in ref_url:
                github_refs.append(ref_url)
            else:
                other_refs.append(ref_url)

        scrape_targets = github_refs[:2] + other_refs[:1]

        for ref_url in scrape_targets:
            try:
                scraped = await self.scraper.scrape(ref_url)
                if scraped and len(scraped) >= self.scraper.min_content_length:
                    label = "GitHub" if "github.com" in ref_url else "Blog/Write-up"
                    parts.append(f"[{label}: {ref_url}]\n{scraped[:3000]}")
                    logger.info(
                        "GHSA enrichment: scraped %s (%d chars)", ref_url, len(scraped)
                    )
            except Exception as exc:
                logger.warning("GHSA enrichment: failed to scrape %s: %s", ref_url, exc)

        return "\n\n---\n\n".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # RSS collection
    # ------------------------------------------------------------------

    async def _collect_rss(self) -> int:
        """Fetch items from configured RSS feeds.

        Returns:
            Number of newly inserted items.
        """
        count = 0
        for feed_url in self.settings.rss_feeds:
            try:
                feed_text = await self.scraper.fetch_text_or_raise(feed_url, timeout_ms=30000)
                feed = feedparser.parse(feed_text)
            except Exception as exc:
                logger.warning("Failed to fetch RSS %s: %s", feed_url, exc)
                continue

            for entry in feed.entries:
                source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
                url = getattr(entry, "link", "")
                title = getattr(entry, "title", "")
                summary = self._clean_summary(getattr(entry, "summary", ""))
                published = (
                    getattr(entry, "published", None)
                    or datetime.now(timezone.utc).isoformat()
                )

                if not self._is_cloud_security_relevant(f"{title} {summary}"):
                    continue

                full_content = ""
                if url:
                    full_content = await self.scraper.scrape(url)

                inserted = await insert_news_item(
                    source_type="rss",
                    source_id=source_id,
                    url=url,
                    title=title,
                    published_at=published,
                    raw_data={
                        "feed_url": feed_url,
                        "title": title,
                        "link": url,
                        "published": published,
                        "summary": summary,
                    },
                    full_content=full_content or None,
                )
                if inserted:
                    count += 1

        return count

    # ------------------------------------------------------------------
    # Twitter/X collection via X API v2
    # ------------------------------------------------------------------

    async def _collect_twitter(self) -> int:
        """Fetch recent tweets from configured handles via X API v2.

        Returns:
            Number of newly inserted items.
        """
        if not self.settings.twitter_bearer_token:
            logger.warning(
                "No X API bearer token configured, skipping Twitter collection"
            )
            return 0

        from_clauses = [f"from:{h}" for h in self.settings.twitter_handles]
        query = f"({' OR '.join(from_clauses)}) -is:retweet"

        users_by_id: dict[str, str] = {}

        count = 0
        next_token: str | None = None
        remaining = self.settings.twitter_max_items

        while remaining > 0:
            params: dict[str, str | int] = {
                "query": query,
                "max_results": max(10, min(remaining, 100)),
                "tweet.fields": "id,text,created_at,author_id,public_metrics,entities",
                "expansions": "author_id",
                "user.fields": "username",
            }
            if next_token:
                params["next_token"] = next_token

            resp = await self._http.get(
                "https://api.x.com/2/tweets/search/recent",
                headers={
                    "Authorization": f"Bearer {self.settings.twitter_bearer_token}",
                },
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()

            for user in body.get("includes", {}).get("users", []):
                users_by_id[user["id"]] = user["username"]

            for tweet in body.get("data", []):
                source_id = tweet["id"]
                author_id = tweet.get("author_id", "")
                username = users_by_id.get(author_id, "")
                url = f"https://x.com/{username}/status/{source_id}" if username else ""
                title = tweet.get("text", "")
                if not self._is_cloud_security_relevant(title):
                    continue
                published = tweet.get(
                    "created_at", datetime.now(timezone.utc).isoformat()
                )
                references = self._extract_tweet_reference_urls(tweet)
                full_content = self._build_tweet_full_content(title, references)

                inserted = await insert_news_item(
                    source_type="twitter",
                    source_id=source_id,
                    url=url,
                    title=title,
                    published_at=published,
                    raw_data={
                        **tweet,
                        "username": username,
                        "references": [{"url": ref} for ref in references],
                    },
                    full_content=full_content,
                )
                if inserted:
                    count += 1

            next_token = body.get("meta", {}).get("next_token")
            result_count = body.get("meta", {}).get("result_count", 0)
            remaining -= result_count

            if not next_token or result_count == 0:
                break

        return count

    # ------------------------------------------------------------------
    # Reddit collection via subreddit RSS feeds
    # ------------------------------------------------------------------

    async def _collect_reddit(self) -> int:
        """Fetch recent posts from configured subreddits via RSS + Reddit JSON."""
        count = 0
        for subreddit in self.settings.reddit_subreddits:
            feed_url = f"https://www.reddit.com/r/{subreddit}/.rss"
            engagement_map = await self._fetch_reddit_engagement(subreddit)
            try:
                feed_text = await self.scraper.fetch_text_or_raise(
                    feed_url,
                    headers={
                        "User-Agent": "BrokenCloudNews/1.0 (cloud-security digest bot)"
                    },
                    timeout_ms=30000,
                )
                feed = feedparser.parse(feed_text)
            except Exception as exc:
                logger.warning("Failed to fetch Reddit feed %s: %s", feed_url, exc)
                continue

            per_sub = 0
            for entry in feed.entries:
                if per_sub >= self.settings.reddit_max_items_per_subreddit:
                    break

                source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
                permalink = str(getattr(entry, "link", "") or "").strip()
                title = getattr(entry, "title", "")
                summary = self._clean_summary(getattr(entry, "summary", ""))
                published = (
                    getattr(entry, "published", None)
                    or datetime.now(timezone.utc).isoformat()
                )

                text_for_filter = f"{title} {summary} r/{subreddit}"
                if not self._is_cloud_security_relevant(text_for_filter):
                    continue
                post_id = self._extract_reddit_post_id(source_id, permalink)
                engagement = engagement_map.get(post_id, {})
                references = self._extract_reddit_reference_urls(permalink, engagement)
                full_content = self._build_reddit_full_content(title, summary, references)

                inserted = await insert_news_item(
                    source_type="reddit",
                    source_id=source_id,
                    url=permalink,
                    title=title,
                    published_at=published,
                    raw_data={
                        "subreddit": subreddit,
                        "feed_url": feed_url,
                        "title": title,
                        "link": permalink,
                        "permalink": permalink,
                        "published": published,
                        "summary": summary,
                        "engagement": engagement,
                        "references": [{"url": ref} for ref in references],
                    },
                    full_content=full_content,
                )
                if inserted:
                    count += 1
                    per_sub += 1

        return count

    async def _fetch_reddit_engagement(
        self, subreddit: str
    ) -> dict[str, dict[str, Any]]:
        """Fetch engagement + outbound URL metadata via Reddit JSON API."""
        url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=100"
        try:
            payload = await self.scraper.fetch_json(
                url,
                headers={
                    "User-Agent": "BrokenCloudNews/1.0 (cloud-security digest bot)"
                },
                timeout_ms=20000,
            )
        except Exception as exc:
            logger.warning("Failed to fetch Reddit metrics %s: %s", url, exc)
            return {}

        out: dict[str, dict[str, Any]] = {}
        children = payload.get("data", {}).get("children", [])
        for child in children:
            data = child.get("data", {})
            post_id = str(data.get("id") or "").strip()
            if not post_id:
                continue
            out[post_id] = {
                "upvotes": float(data.get("ups") or data.get("score") or 0),
                "comments": float(data.get("num_comments") or 0),
                "upvote_ratio": float(data.get("upvote_ratio") or 0),
                "url": str(data.get("url") or "").strip(),
                "url_overridden_by_dest": str(
                    data.get("url_overridden_by_dest") or ""
                ).strip(),
                "permalink": self._normalize_reddit_permalink(
                    str(data.get("permalink") or "").strip()
                ),
            }
        return out

    @staticmethod
    def _extract_reddit_post_id(source_id: str, url: str) -> str:
        """Extract Reddit post id from feed source id or permalink."""
        sid = (source_id or "").strip()
        if sid.startswith("t3_"):
            return sid[3:]

        match = re.search(r"/comments/([a-z0-9]+)/", url or "", flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
        return ""

    @staticmethod
    def _extract_tweet_reference_urls(tweet: dict) -> list[str]:
        """Extract external reference URLs from tweet entities."""
        entities = tweet.get("entities", {}) if isinstance(tweet, dict) else {}
        urls = entities.get("urls", []) if isinstance(entities, dict) else []
        seen: set[str] = set()
        out: list[str] = []

        for item in urls:
            if isinstance(item, dict):
                candidates = [
                    item.get("unwound_url", ""),
                    item.get("expanded_url", ""),
                    item.get("url", ""),
                ]
            else:
                candidates = [str(item)]

            for candidate in candidates:
                url = str(candidate or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                if CollectorExecutor._is_internal_twitter_url(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                out.append(url)
                break

        return out

    @staticmethod
    def _is_internal_twitter_url(url: str) -> bool:
        """Return whether URL points back to X/Twitter itself."""
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in {"x.com", "twitter.com", "mobile.twitter.com", "t.co"}

    @staticmethod
    def _extract_reddit_reference_urls(permalink: str, metadata: dict[str, Any]) -> list[str]:
        """Extract non-Reddit outbound URLs from Reddit post metadata."""
        permalink_norm = (permalink or "").strip().rstrip("/")
        candidates = [
            metadata.get("url_overridden_by_dest", ""),
            metadata.get("url", ""),
        ]
        seen: set[str] = set()
        out: list[str] = []

        for candidate in candidates:
            url = str(candidate or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if CollectorExecutor._is_internal_reddit_url(url):
                continue
            if url.rstrip("/") == permalink_norm:
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)

        return out

    @staticmethod
    def _select_reddit_primary_url(
        permalink: str,
        references: list[str],
        *,
        title: str = "",
        summary: str = "",
    ) -> str:
        """Prefer outbound source only when likely technically useful."""
        for ref in references:
            if CollectorExecutor._is_useful_reddit_reference(ref, title, summary):
                return ref
        return (permalink or "").strip()

    @staticmethod
    def _is_internal_reddit_url(url: str) -> bool:
        """Return whether URL points to Reddit-owned domains."""
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in {
            "reddit.com",
            "old.reddit.com",
            "new.reddit.com",
            "np.reddit.com",
            "redd.it",
            "i.redd.it",
            "v.redd.it",
            "redditmedia.com",
        }

    @staticmethod
    def _is_useful_reddit_reference(url: str, title: str, summary: str) -> bool:
        """Heuristic: outbound links must look technical to replace permalink."""
        if not url.startswith(("http://", "https://")):
            return False
        if CollectorExecutor._is_internal_reddit_url(url):
            return False

        try:
            parsed = urlparse(url)
        except Exception:
            return False

        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return False
        if CollectorExecutor._host_matches(host, _REDDIT_LOW_SIGNAL_DOMAINS):
            return False
        if CollectorExecutor._host_matches(host, _REDDIT_TECHNICAL_DOMAIN_HINTS):
            return True

        url_text = f"{host}{parsed.path} {parsed.query}".lower()
        context_text = f"{title} {summary}".lower()
        technical_hits = 0
        if any(hint in url_text for hint in _REDDIT_TECHNICAL_HINTS):
            technical_hits += 1
        if any(hint in context_text for hint in _REDDIT_TECHNICAL_HINTS):
            technical_hits += 1
        return technical_hits >= 2

    @staticmethod
    def _host_matches(host: str, domains: frozenset[str]) -> bool:
        """Return True when host equals or is subdomain of a listed domain."""
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    @staticmethod
    def _normalize_reddit_permalink(permalink: str) -> str:
        """Normalize Reddit relative permalinks to absolute URLs."""
        text = (permalink or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://")):
            return text
        if text.startswith("/"):
            return f"https://www.reddit.com{text}"
        return f"https://www.reddit.com/{text}"

    @staticmethod
    def _build_reddit_full_content(
        title: str,
        summary: str,
        references: list[str],
    ) -> str | None:
        """Compose analysis-friendly Reddit content with outbound references."""
        text = (summary or "").strip()
        if text.lower().startswith("submitted by "):
            text = ""
        if not text:
            text = (title or "").strip()
        if not references:
            return text or None

        refs_block = "\n".join(f"- {ref}" for ref in references[:6])
        if not text:
            return f"Reference links:\n{refs_block}"
        return f"{text}\n\nReference links:\n{refs_block}"

    @staticmethod
    def _build_tweet_full_content(tweet_text: str, references: list[str]) -> str | None:
        """Compose analysis-friendly tweet content with extracted references."""
        text = (tweet_text or "").strip()
        if not references:
            return text or None

        refs_block = "\n".join(f"- {ref}" for ref in references[:6])
        if not text:
            return f"Reference links:\n{refs_block}"
        return f"{text}\n\nReference links:\n{refs_block}"
