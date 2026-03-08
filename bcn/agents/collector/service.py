"""Collector domain service shared by the control plane and legacy agent."""

from __future__ import annotations

import calendar
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

from bcn.common.config import Settings
from bcn.common.models import CollectedNewsItem
from bcn.common.scraper import Scraper

logger = logging.getLogger(__name__)

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


class CollectorService:
    """Domain service for source-specific crawl and normalization logic."""

    def __init__(
        self,
        settings: Settings,
        *,
        scraper: Scraper | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_scraper = scraper is None
        self._owns_http_client = http_client is None
        self.scraper = scraper if scraper is not None else Scraper(
            content_limit=settings.scrape_content_limit,
            min_content_length=settings.scrape_min_content_length,
        )
        self._http = (
            http_client if http_client is not None else httpx.AsyncClient(timeout=60)
        )

    async def close(self) -> None:
        """Release resources owned by this collector service."""
        if self._owns_scraper:
            await self.scraper.close()
        if self._owns_http_client:
            await self._http.aclose()

    def _is_cloud_security_relevant(self, text: str) -> bool:
        """Return whether the text looks relevant to cloud security."""
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized or len(normalized) < 20:
            return False

        required = self.settings.twitter_required_keywords
        if required and not any(kw.lower() in normalized for kw in required):
            return False

        if "ctf" in normalized and not any(
            token in normalized for token in ("cloud", "k8s", "container", "cve", "vuln")
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

    @staticmethod
    def _feed_entry_value(entry: Any, field: str) -> Any:
        """Read one feed field without feedparser's attribute alias fallback."""
        if hasattr(entry, "get"):
            return entry.get(field)
        return getattr(entry, field, None)

    @staticmethod
    def _coerce_feed_datetime(value: Any) -> datetime | None:
        """Normalize feed date values into aware UTC datetimes."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                try:
                    dt = parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    return None
        elif hasattr(value, "tm_year") and hasattr(value, "tm_mon"):
            dt = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
        else:
            return None

        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _extract_feed_published_at(self, entry: Any) -> tuple[datetime, str | None, str]:
        """Return the best available feed timestamp for one entry."""
        for field in (
            "published_parsed",
            "updated_parsed",
            "created_parsed",
            "published",
            "updated",
            "created",
        ):
            raw_value = self._feed_entry_value(entry, field)
            published_at = self._coerce_feed_datetime(raw_value)
            if published_at is not None:
                raw_text = raw_value if isinstance(raw_value, str) else None
                return published_at, raw_text, field
        return datetime.now(timezone.utc), None, "fallback_now"

    async def collect_ghsa_items(self) -> list[CollectedNewsItem]:
        """Fetch GitHub Security Advisories matching cloud keywords."""
        if not self.settings.github_token:
            logger.warning("No GitHub token configured, skipping GHSA collection")
            return []

        response = await self._http.post(
            "https://api.github.com/graphql",
            headers={
                "Authorization": f"Bearer {self.settings.github_token}",
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
            re.compile(keyword, re.IGNORECASE)
            for keyword in self.settings.ghsa_keywords
        ]
        allowed = set(self.settings.ghsa_severities)

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
            full_content = await self._enrich_ghsa_content(item, references)
            items.append(
                CollectedNewsItem(
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
            )

        return items

    async def _enrich_ghsa_content(
        self,
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
            if any(domain in ref_url for domain in _SKIP_SCRAPE_DOMAINS):
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

    async def collect_rss_items(self) -> list[CollectedNewsItem]:
        """Fetch items from configured RSS feeds."""
        items: list[CollectedNewsItem] = []
        for feed_url in self.settings.rss_feeds:
            try:
                feed_text = await self.scraper.fetch_text_or_raise(
                    feed_url,
                    timeout_ms=30000,
                )
                feed = feedparser.parse(feed_text)
            except Exception as exc:
                logger.warning("Failed to fetch RSS %s: %s", feed_url, exc)
                continue

            for entry in feed.entries:
                source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
                url = getattr(entry, "link", "")
                title = getattr(entry, "title", "")
                summary = self._clean_summary(getattr(entry, "summary", ""))
                published_at, published_raw, published_field = (
                    self._extract_feed_published_at(entry)
                )
                published = published_at.isoformat()

                if not self._is_cloud_security_relevant(f"{title} {summary}"):
                    continue

                full_content = ""
                if url:
                    full_content = await self.scraper.scrape(url)

                items.append(
                    CollectedNewsItem(
                        source_type="rss",
                        source_id=source_id,
                        url=url,
                        title=title,
                        published_at=published_at,
                        raw_data={
                            "feed_url": feed_url,
                            "title": title,
                            "link": url,
                            "published": published,
                            "published_raw": published_raw,
                            "published_field": published_field,
                            "summary": summary,
                        },
                        full_content=full_content or None,
                    )
                )

        return items

    async def collect_twitter_items(self) -> list[CollectedNewsItem]:
        """Fetch recent tweets from configured handles via X API v2."""
        if not self.settings.twitter_bearer_token:
            logger.warning(
                "No X API bearer token configured, skipping Twitter collection"
            )
            return []

        from_clauses = [f"from:{handle}" for handle in self.settings.twitter_handles]
        query = f"({' OR '.join(from_clauses)}) -is:retweet"
        users_by_id: dict[str, str] = {}
        items: list[CollectedNewsItem] = []

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

            response = await self._http.get(
                "https://api.x.com/2/tweets/search/recent",
                headers={
                    "Authorization": (
                        f"Bearer {self.settings.twitter_bearer_token}"
                    ),
                },
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()

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
                items.append(
                    CollectedNewsItem(
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
                )

            next_token = body.get("meta", {}).get("next_token")
            result_count = body.get("meta", {}).get("result_count", 0)
            remaining -= result_count
            if not next_token or result_count == 0:
                break

        return items

    async def collect_reddit_items(self) -> list[CollectedNewsItem]:
        """Fetch recent posts from configured subreddits via RSS + Reddit JSON."""
        items: list[CollectedNewsItem] = []
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

            for entry in feed.entries:
                source_id = getattr(entry, "id", None) or getattr(entry, "link", "")
                permalink = str(getattr(entry, "link", "") or "").strip()
                title = getattr(entry, "title", "")
                summary = self._clean_summary(getattr(entry, "summary", ""))
                published_at, published_raw, published_field = (
                    self._extract_feed_published_at(entry)
                )
                published = published_at.isoformat()

                text_for_filter = f"{title} {summary} r/{subreddit}"
                if not self._is_cloud_security_relevant(text_for_filter):
                    continue

                post_id = self._extract_reddit_post_id(source_id, permalink)
                engagement = engagement_map.get(post_id, {})
                references = self._extract_reddit_reference_urls(permalink, engagement)
                full_content = self._build_reddit_full_content(
                    title,
                    summary,
                    references,
                )
                items.append(
                    CollectedNewsItem(
                        source_type="reddit",
                        source_id=source_id,
                        url=permalink,
                        title=title,
                        published_at=published_at,
                        raw_data={
                            "subreddit": subreddit,
                            "feed_url": feed_url,
                            "title": title,
                            "link": permalink,
                            "permalink": permalink,
                            "published": published,
                            "published_raw": published_raw,
                            "published_field": published_field,
                            "summary": summary,
                            "engagement": engagement,
                            "references": [{"url": ref} for ref in references],
                        },
                        full_content=full_content,
                    )
                )

        return items

    async def _fetch_reddit_engagement(
        self,
        subreddit: str,
    ) -> dict[str, dict[str, Any]]:
        """Fetch engagement and outbound URL metadata via the Reddit JSON API."""
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
        """Extract the Reddit post id from a feed source id or permalink."""
        sid = (source_id or "").strip()
        if sid.startswith("t3_"):
            return sid[3:]

        match = re.search(r"/comments/([a-z0-9]+)/", url or "", flags=re.IGNORECASE)
        if match:
            return match.group(1).lower()
        return ""

    @staticmethod
    def _extract_tweet_reference_urls(tweet: dict[str, Any]) -> list[str]:
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
                if CollectorService._is_internal_twitter_url(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                out.append(url)
                break

        return out

    @staticmethod
    def _is_internal_twitter_url(url: str) -> bool:
        """Return whether a URL points back to X/Twitter itself."""
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in {"x.com", "twitter.com", "mobile.twitter.com", "t.co"}

    @staticmethod
    def _extract_reddit_reference_urls(
        permalink: str,
        metadata: dict[str, Any],
    ) -> list[str]:
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
            if CollectorService._is_internal_reddit_url(url):
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
        """Prefer outbound source only when it looks technically useful."""
        for reference in references:
            if CollectorService._is_useful_reddit_reference(reference, title, summary):
                return reference
        return (permalink or "").strip()

    @staticmethod
    def _is_internal_reddit_url(url: str) -> bool:
        """Return whether a URL points to Reddit-owned domains."""
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
        """Return whether an outbound Reddit link looks technically useful."""
        if not url.startswith(("http://", "https://")):
            return False
        if CollectorService._is_internal_reddit_url(url):
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
        if CollectorService._host_matches(host, _REDDIT_LOW_SIGNAL_DOMAINS):
            return False
        if CollectorService._host_matches(host, _REDDIT_TECHNICAL_DOMAIN_HINTS):
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
        """Return whether a host equals or is a subdomain of a listed domain."""
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

        refs_block = "\n".join(f"- {reference}" for reference in references[:6])
        if not text:
            return f"Reference links:\n{refs_block}"
        return f"{text}\n\nReference links:\n{refs_block}"

    @staticmethod
    def _build_tweet_full_content(
        tweet_text: str,
        references: list[str],
    ) -> str | None:
        """Compose analysis-friendly tweet content with extracted references."""
        text = (tweet_text or "").strip()
        if not references:
            return text or None

        refs_block = "\n".join(f"- {reference}" for reference in references[:6])
        if not text:
            return f"Reference links:\n{refs_block}"
        return f"{text}\n\nReference links:\n{refs_block}"


__all__ = [
    "CollectorService",
]
