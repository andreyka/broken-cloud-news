"""Item selection and ranking for daily briefings."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from datetime import timezone
import difflib
import math
import re
from urllib.parse import urlparse

from bcn.briefing.text import canonical_url_key
from bcn.briefing.text import to_dict
from bcn.common.config import Settings

_TRUSTED_RSS_DOMAINS = frozenset(
    {
        "cisa.gov",
        "aws.amazon.com",
        "blog.cloudflare.com",
        "cloud.google.com",
        "security.googleblog.com",
        "azure.microsoft.com",
        "microsoft.com",
        "github.blog",
        "kubernetes.io",
        "ubuntu.com",
        "unit42.paloaltonetworks.com",
        "research.checkpoint.com",
    }
)

_CSP_SIDE_DOMAINS = frozenset(
    {
        "blog.cloudflare.com",
        "cloudflare.com",
        "aws.amazon.com",
        "azure.microsoft.com",
        "microsoft.com",
        "cloud.google.com",
        "security.googleblog.com",
        "github.blog",
        "kubernetes.io",
    }
)

_ISSUE_ID_RE = re.compile(
    r"\b(?:cve-\d{4}-\d+|ghsa-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})\b"
)
_TOPIC_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "with",
        "into",
        "over",
        "after",
        "before",
        "about",
        "this",
        "that",
        "onto",
        "cloud",
        "security",
        "vulnerability",
        "vulnerabilities",
        "issue",
        "issues",
        "patch",
        "patches",
        "advisory",
        "advisories",
        "exploit",
        "exploits",
        "fix",
        "fixes",
        "update",
        "updates",
        "attack",
        "attacks",
        "remote",
        "code",
        "execution",
        "allow",
        "allows",
        "new",
        "latest",
        "reported",
        "report",
        "today",
        "guide",
        "analysis",
        "risk",
        "risks",
        "zero",
        "wild",
        "active",
        "actively",
        "campaign",
        "campaigns",
        "threat",
        "threats",
    }
)


class BriefingSelector:
    """Selects a diverse, actionable set of items for briefing generation."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def select_items(
        self,
        items: list[dict],
        recent_published: list[dict] | None = None,
        *,
        quiet_mode: bool = False,
    ) -> list[dict]:
        """Select a diverse set of actionable items for the daily briefing."""
        recent_items = recent_published or []
        ranked = sorted(
            items,
            key=lambda i: (
                self.priority_score(i, recent_items),
                int(i.get("relevance_score", 0)),
                self._parse_timestamp(i.get("published_at")),
            ),
            reverse=True,
        )

        max_items = (
            min(
                self.settings.briefing_max_items,
                self.settings.briefing_quiet_day_max_items,
            )
            if quiet_mode
            else self.settings.briefing_max_items
        )
        max_ai = self.settings.briefing_max_ai_items
        max_twitter = self.settings.briefing_max_twitter_items
        max_rss = self.settings.briefing_max_rss_items
        max_per_domain = self.settings.briefing_max_items_per_domain
        min_selected = max(1, int(self.settings.briefing_min_selected_items))
        mix_targets = self.mix_targets(quiet_mode)

        selected: list[dict] = []
        selected_ids: set[str] = set()
        source_counts: Counter[str] = Counter()
        domain_counts: Counter[str] = Counter()
        bucket_counts: Counter[str] = Counter()
        ai_count = 0

        def can_add(item: dict, *, relax_soft_limits: bool = False) -> bool:
            nonlocal ai_count
            item_id = str(item.get("id"))
            if item_id in selected_ids:
                return False

            if self.is_duplicate_of(item, selected):
                return False

            if self.is_duplicate_of(item, recent_items):
                return False

            if not self.passes_source_floor(item) and not relax_soft_limits:
                return False
            source = str(item.get("source_type", "")).lower()
            if (
                source == "twitter"
                and source_counts[source] >= max_twitter
                and not relax_soft_limits
            ):
                return False
            if source == "rss" and source_counts[source] >= max_rss:
                return False
            domain = self._extract_domain(str(item.get("url", "")))
            if (
                domain
                and max_per_domain > 0
                and domain_counts[domain] >= max_per_domain
            ):
                return False
            if self.is_ai_heavy(item) and ai_count >= max_ai and not relax_soft_limits:
                return False
            return True

        def add(item: dict) -> None:
            nonlocal ai_count
            item_id = str(item.get("id"))
            selected.append(item)
            selected_ids.add(item_id)
            source = str(item.get("source_type", "")).lower()
            source_counts[source] += 1
            domain = self._extract_domain(str(item.get("url", "")))
            if domain:
                domain_counts[domain] += 1
            bucket_counts[self.classify_bucket(item)] += 1
            if self.is_ai_heavy(item):
                ai_count += 1

        for bucket, target in mix_targets.items():
            if target <= 0:
                continue
            while bucket_counts[bucket] < target and len(selected) < max_items:
                candidate = None
                for item in ranked:
                    if self.classify_bucket(item) != bucket:
                        continue
                    if not can_add(item):
                        continue
                    candidate = item
                    break
                if not candidate:
                    break
                add(candidate)

        for source in ("ghsa", "reddit", "rss", "twitter"):
            for item in ranked:
                if len(selected) >= max_items:
                    break
                if str(item.get("source_type", "")).lower() != source:
                    continue
                if not can_add(item):
                    continue
                add(item)
                break

        for item in ranked:
            if len(selected) >= max_items:
                break
            if not self.is_actionable(item):
                continue
            if can_add(item):
                add(item)

        for item in ranked:
            if len(selected) >= max_items:
                break
            if can_add(item):
                add(item)

        if len(selected) < max_items:
            for item in ranked:
                if len(selected) >= max_items:
                    break
                if can_add(item, relax_soft_limits=True):
                    add(item)

        if len(selected) < min(max_items, min_selected):
            for item in ranked:
                if len(selected) >= max_items:
                    break
                item_id = str(item.get("id"))
                if item_id in selected_ids:
                    continue
                add(item)

        constrained = self._enforce_hard_mix_constraints(
            selected=selected,
            ranked=ranked,
            max_items=max_items,
            max_per_domain=max_per_domain,
            quiet_mode=quiet_mode,
        )
        return constrained

    def _enforce_hard_mix_constraints(
        self,
        *,
        selected: list[dict],
        ranked: list[dict],
        max_items: int,
        max_per_domain: int,
        quiet_mode: bool,
    ) -> list[dict]:
        """Apply hard editorial constraints; return [] if unsatisfied."""
        if not selected:
            return []

        source_limit = self._source_limit(max_items)
        min_selected = max(1, int(self.settings.briefing_min_selected_items))
        enforce_mix_requirements = (
            (not quiet_mode)
            and max_items >= 3
            and min_selected >= 2
            and len(ranked) >= 2
        )
        require_reddit = (
            bool(self.settings.briefing_selection_require_reddit)
            and enforce_mix_requirements
        )
        require_csp = (
            bool(self.settings.briefing_selection_require_csp)
            and enforce_mix_requirements
        )

        out = list(selected[:max_items])
        selected_ids = {str(i.get("id")) for i in out}
        source_counts = Counter(str(i.get("source_type", "")).lower() for i in out)
        domain_counts = Counter(
            self._extract_domain(str(i.get("url", ""))) for i in out
        )
        if "" in domain_counts:
            del domain_counts[""]

        def item_rank(item: dict) -> tuple[float, float, datetime]:
            return (
                self.priority_score(item),
                float(int(item.get("relevance_score", 0))),
                self._parse_timestamp(item.get("published_at")),
            )

        def can_add(item: dict) -> bool:
            item_id = str(item.get("id"))
            if item_id in selected_ids:
                return False
            source = str(item.get("source_type", "")).lower()
            domain = self._extract_domain(str(item.get("url", "")))
            if source_counts[source] >= source_limit:
                return False
            if (
                domain
                and max_per_domain > 0
                and domain_counts[domain] >= max_per_domain
            ):
                return False
            return True

        def add(item: dict) -> bool:
            if not can_add(item):
                return False
            out.append(item)
            selected_ids.add(str(item.get("id")))
            source = str(item.get("source_type", "")).lower()
            source_counts[source] += 1
            domain = self._extract_domain(str(item.get("url", "")))
            if domain:
                domain_counts[domain] += 1
            return True

        def remove(item: dict) -> None:
            out.remove(item)
            selected_ids.discard(str(item.get("id")))
            source = str(item.get("source_type", "")).lower()
            source_counts[source] -= 1
            if source_counts[source] <= 0:
                del source_counts[source]
            domain = self._extract_domain(str(item.get("url", "")))
            if domain:
                domain_counts[domain] -= 1
                if domain_counts[domain] <= 0:
                    del domain_counts[domain]

        def weakest(candidates: list[dict]) -> dict | None:
            if not candidates:
                return None
            return sorted(candidates, key=item_rank)[0]

        def has_reddit() -> bool:
            return any(str(i.get("source_type", "")).lower() == "reddit" for i in out)

        def has_csp() -> bool:
            return any(self.is_csp_side_item(i) for i in out)

        # Enforce per-domain cap strictly.
        while True:
            too_many_domain = next(
                (
                    d
                    for d, n in domain_counts.items()
                    if max_per_domain > 0 and n > max_per_domain
                ),
                None,
            )
            if not too_many_domain:
                break
            candidates = [
                i
                for i in out
                if self._extract_domain(str(i.get("url", ""))) == too_many_domain
            ]
            victim = weakest(candidates)
            if not victim:
                break
            remove(victim)

        # Enforce source-share cap.
        while True:
            too_many_source = next(
                (s for s, n in source_counts.items() if n > source_limit), None
            )
            if not too_many_source:
                break
            candidates = [
                i
                for i in out
                if str(i.get("source_type", "")).lower() == too_many_source
            ]
            victim = weakest(candidates)
            if not victim:
                break
            remove(victim)

        # Add required Reddit item.
        if require_reddit and not has_reddit():
            reddit_candidates = [
                i
                for i in ranked
                if str(i.get("source_type", "")).lower() == "reddit"
                and self.passes_source_floor(i)
            ]
            if not reddit_candidates:
                return []

            if len(out) >= max_items:
                protected_csp = (
                    has_csp() and sum(1 for i in out if self.is_csp_side_item(i)) == 1
                )
                removable = [
                    i
                    for i in out
                    if str(i.get("source_type", "")).lower() != "reddit"
                    and (not protected_csp or not self.is_csp_side_item(i))
                ]
                victim = weakest(removable)
                if victim:
                    remove(victim)

            inserted = False
            for candidate in reddit_candidates:
                if add(candidate):
                    inserted = True
                    break
            if not inserted:
                return []

        # Add required Cloudflare/CSP-side item.
        if require_csp and not has_csp():
            csp_candidates = [
                i
                for i in ranked
                if self.is_csp_side_item(i) and self.passes_source_floor(i)
            ]
            if not csp_candidates:
                return []

            if len(out) >= max_items:
                removable = [i for i in out if not self.is_csp_side_item(i)]
                if (
                    require_reddit
                    and has_reddit()
                    and sum(
                        1
                        for i in out
                        if str(i.get("source_type", "")).lower() == "reddit"
                    )
                    == 1
                ):
                    removable = [
                        i
                        for i in removable
                        if str(i.get("source_type", "")).lower() != "reddit"
                    ]
                victim = weakest(removable)
                if victim:
                    remove(victim)

            inserted = False
            for candidate in csp_candidates:
                if add(candidate):
                    inserted = True
                    break
            if not inserted:
                return []

        # Re-fill up to max_items under hard constraints.
        for item in ranked:
            if len(out) >= max_items:
                break
            add(item)

        # Final hard checks.
        if require_reddit and not has_reddit():
            return []
        if require_csp and not has_csp():
            return []
        if any(n > source_limit for n in source_counts.values()):
            return []
        if any(
            max_per_domain > 0 and n > max_per_domain for n in domain_counts.values()
        ):
            return []

        return out

    def _source_limit(self, max_items: int) -> int:
        share = float(self.settings.briefing_max_source_share)
        share = min(1.0, max(0.2, share))
        return max(1, int(math.floor(max_items * share)))

    def priority_score(
        self, item: dict, recent_published: list[dict] | None = None
    ) -> float:
        """Composite priority from relevance, exploitability, trust, engagement, novelty."""
        relevance = float(int(item.get("relevance_score", 0)))
        score = relevance
        score += self.engagement_bonus(item)
        score += self.exploitability_bonus(item)
        score += self.source_trust_bonus(item)
        score -= self.novelty_penalty(item, recent_published or [])
        return score

    def mix_targets(self, quiet_mode: bool) -> dict[str, int]:
        """Per-digest category mix targets."""
        if quiet_mode:
            return {
                "urgent_threats": 1,
                "platform_changes": 1,
                "tooling_use_case": 0,
                "regulatory_legal": 0,
            }
        return {
            "urgent_threats": self.settings.briefing_mix_min_urgent,
            "platform_changes": self.settings.briefing_mix_min_platform,
            "tooling_use_case": self.settings.briefing_mix_min_tooling,
            "regulatory_legal": self.settings.briefing_mix_min_regulatory,
        }

    def engagement_bonus(self, item: dict) -> float:
        """Compute capped source-specific social-proof bonus for ranking."""
        max_bonus = max(0.0, float(self.settings.briefing_social_proof_max_bonus))
        weight = max(0.0, float(self.settings.briefing_social_proof_weight))
        if max_bonus <= 0 or weight <= 0:
            return 0.0

        raw_score = self.engagement_raw_score(item)
        if raw_score <= 0:
            return 0.0

        bonus = math.log1p(raw_score) * weight
        return min(max_bonus, bonus)

    def engagement_raw_score(self, item: dict) -> float:
        """Extract source-specific engagement magnitude from raw payload."""
        source = str(item.get("source_type", "")).lower()
        raw = to_dict(item.get("raw_data"))

        if source == "twitter":
            metrics = raw.get("public_metrics", {}) if isinstance(raw, dict) else {}
            likes = float(metrics.get("like_count") or 0)
            reposts = float(metrics.get("retweet_count") or 0)
            replies = float(metrics.get("reply_count") or 0)
            quotes = float(metrics.get("quote_count") or 0)
            return likes + (2.0 * reposts) + (1.2 * replies) + (2.0 * quotes)

        if source == "reddit":
            engagement = raw.get("engagement", {}) if isinstance(raw, dict) else {}
            upvotes = float(engagement.get("upvotes") or 0)
            comments = float(engagement.get("comments") or 0)
            score = upvotes + (2.0 * comments)
            if score > 0:
                return score

            summary = str(raw.get("summary", ""))
            points_m = re.search(r"(\d+)\s+points?", summary, flags=re.IGNORECASE)
            comments_m = re.search(r"(\d+)\s+comments?", summary, flags=re.IGNORECASE)
            fallback_points = float(points_m.group(1)) if points_m else 0.0
            fallback_comments = float(comments_m.group(1)) if comments_m else 0.0
            return fallback_points + (2.0 * fallback_comments)

        return 0.0

    @staticmethod
    def exploitability_bonus(item: dict) -> float:
        """Boost concrete exploit/patch items over generic commentary."""
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        score = 0.0
        if re.search(r"(cve-\d{4}-\d+|ghsa-)", text):
            score += 1.35
        if re.search(
            r"(actively exploited|in the wild|zero[-\s]?day|rce|auth bypass|ssrf|privesc|container escape)",
            text,
        ):
            score += 0.95
        if re.search(r"(patch|fixed|mitigation|detection|ioc|rule|playbook)", text):
            score += 0.55
        return min(2.5, score)

    def source_trust_bonus(self, item: dict) -> float:
        """Prefer durable primary sources while keeping social discovery."""
        source = str(item.get("source_type", "")).lower()
        if source == "ghsa":
            return 1.5
        if source == "rss":
            return 0.8 if self.is_trusted_rss_item(item) else 0.0
        if source == "reddit":
            return 0.25
        if source == "twitter":
            return 0.2
        return 0.0

    def is_duplicate_of(self, item: dict, others: list[dict]) -> bool:
        """Strictly determine if an item is a duplicate of other items."""
        if not others:
            return False

        cur_url = canonical_url_key(str(item.get("url", "")))
        cur_title = self._normalize_title(str(item.get("title", "")))
        cur_issue_keys = self._issue_keys(item)
        threshold = float(self.settings.briefing_novelty_title_similarity_threshold)

        for other in others:
            other_url = canonical_url_key(str(other.get("url", "")))
            if cur_url and other_url and cur_url == other_url:
                return True

            other_title = self._normalize_title(str(other.get("title", "")))
            if cur_title and other_title:
                sim = difflib.SequenceMatcher(None, cur_title, other_title).ratio()
                if sim >= threshold:
                    return True

            if cur_issue_keys:
                other_issue_keys = self._issue_keys(other)
                if other_issue_keys and len(cur_issue_keys & other_issue_keys) > 0:
                    return True

        return False

    def novelty_penalty(self, item: dict, recent_published: list[dict]) -> float:
        """Penalize near-duplicate stories from recent distributed digests."""
        if not recent_published:
            return 0.0

        cur_url = canonical_url_key(str(item.get("url", "")))
        cur_title = self._normalize_title(str(item.get("title", "")))
        cur_issue_keys = self._issue_keys(item)
        best_title_similarity = 0.0
        best_issue_overlap = 0.0
        same_url_seen = False

        for prev in recent_published:
            prev_url = canonical_url_key(str(prev.get("url", "")))
            if cur_url and prev_url and cur_url == prev_url:
                same_url_seen = True
                best_title_similarity = 1.0
                break

            if cur_issue_keys:
                prev_issue_keys = self._issue_keys(prev)
                if prev_issue_keys:
                    overlap = len(cur_issue_keys & prev_issue_keys)
                    if overlap > 0:
                        norm = max(1, min(len(cur_issue_keys), len(prev_issue_keys)))
                        best_issue_overlap = max(best_issue_overlap, overlap / norm)

            prev_title = self._normalize_title(str(prev.get("title", "")))
            if not cur_title or not prev_title:
                continue
            sim = difflib.SequenceMatcher(None, cur_title, prev_title).ratio()
            if sim > best_title_similarity:
                best_title_similarity = sim

        if same_url_seen:
            return 3.0

        threshold = float(self.settings.briefing_novelty_title_similarity_threshold)
        similarity_penalty = 0.0
        if best_title_similarity >= threshold:
            span = max(0.01, 1.0 - threshold)
            scaled = (best_title_similarity - threshold) / span
            similarity_penalty = min(2.3, 0.7 + (scaled * 1.6))

        recurrence_penalty = 0.0
        if best_issue_overlap > 0.0:
            recurrence_penalty = min(0.75, 0.35 + (best_issue_overlap * 0.4))

        return min(3.0, similarity_penalty + recurrence_penalty)

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize titles before similarity checks."""
        title = title.lower().strip()
        title = re.sub(r"https?://\S+", "", title)
        title = re.sub(r"[^a-z0-9\s\-_/.:]", " ", title)
        return re.sub(r"\s+", " ", title).strip()

    def _issue_keys(self, item: dict) -> set[str]:
        """Extract recurrence keys for duplicate-topic suppression."""
        text = self._normalize_title(
            f"{item.get('title', '')} {item.get('summary', '')}"
        )
        if not text:
            return set()

        keys = {m.group(0).lower() for m in _ISSUE_ID_RE.finditer(text)}
        signature = self._topic_signature(text)
        if signature:
            keys.add(signature)
        return keys

    @staticmethod
    def _topic_signature(normalized_text: str) -> str:
        tokens = re.findall(r"[a-z0-9]{3,}", normalized_text)
        filtered = [
            tok for tok in tokens if tok not in _TOPIC_STOPWORDS and not tok.isdigit()
        ]
        if len(filtered) < 2:
            return ""

        counts = Counter(filtered)
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        top_tokens = sorted(tok for tok, _ in ranked[:3])
        if len(top_tokens) < 2:
            return ""
        return "topic:" + "+".join(top_tokens)

    @staticmethod
    def classify_bucket(item: dict) -> str:
        """Map an item into editorial mix buckets."""
        text = (
            f"{item.get('title', '')} {item.get('summary', '')} "
            f"{' '.join(item.get('ai_tags') or [])}"
        ).lower()

        if re.search(
            r"(cve-\d{4}-\d+|ghsa-|actively exploited|in the wild|incident|breach|"
            r"rce|auth bypass|ssrf|container escape|ddos|exploit|malware)",
            text,
        ):
            return "urgent_threats"
        if re.search(
            r"(law|regulat|compliance|pci|dora|nis2|sec\b|gdpr|privacy act|order|mandate)",
            text,
        ):
            return "regulatory_legal"
        if re.search(
            r"(aws|azure|gcp|cloudflare|kubernetes|k8s|iam|terraform|serverless|"
            r"envoy|postgres|clickhouse|redis|qemu|kvm|load balancer|control plane)",
            text,
        ):
            return "platform_changes"
        return "tooling_use_case"

    def passes_source_floor(self, item: dict) -> bool:
        """Drop weak social/untrusted items unless relevance is very high."""
        source = str(item.get("source_type", "")).lower()
        relevance = int(item.get("relevance_score", 0))
        if relevance >= int(self.settings.briefing_social_floor_exempt_relevance):
            return True

        if source == "reddit":
            return self.engagement_raw_score(item) >= float(
                self.settings.briefing_min_reddit_engagement_score
            )

        if source == "twitter":
            return self.engagement_raw_score(item) >= float(
                self.settings.briefing_min_twitter_engagement_score
            )

        if source == "rss" and not self.is_trusted_rss_item(item):
            return relevance >= int(self.settings.briefing_untrusted_rss_min_score)

        return True

    def is_trusted_rss_item(self, item: dict) -> bool:
        """Determine whether RSS item comes from a trusted source domain."""
        raw = to_dict(item.get("raw_data"))
        url_domain = self._extract_domain(str(item.get("url", "")))
        feed_domain = self._extract_domain(str(raw.get("feed_url", "")))
        for domain in (url_domain, feed_domain):
            if not domain:
                continue
            if any(
                domain == d or domain.endswith(f".{d}") for d in _TRUSTED_RSS_DOMAINS
            ):
                return True
        return False

    def is_csp_side_item(self, item: dict) -> bool:
        """Return whether item appears to originate from Cloudflare/CSP-side signal."""
        domain = self._extract_domain(str(item.get("url", "")))
        if domain and any(
            domain == d or domain.endswith(f".{d}") for d in _CSP_SIDE_DOMAINS
        ):
            return True

        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        return bool(re.search(r"\b(cloudflare|aws|azure|gcp|google cloud)\b", text))

    def is_quiet_day(self, items: list[dict]) -> bool:
        """Detect low-signal days and switch to deeper fewer-item mode."""
        if not self.settings.briefing_quiet_day_enabled:
            return False

        min_items = int(self.settings.briefing_quiet_day_min_high_signal_items)
        return self.high_signal_count(items) < min_items

    def high_signal_count(self, items: list[dict]) -> int:
        """Count high-signal actionable items (or GHSA advisories)."""
        threshold = int(self.settings.briefing_quiet_day_high_signal_threshold)
        high_signal = 0
        for item in items:
            relevance = int(item.get("relevance_score", 0))
            if relevance < threshold:
                continue
            if (
                self.is_actionable(item)
                or str(item.get("source_type", "")).lower() == "ghsa"
            ):
                high_signal += 1
        return high_signal

    @staticmethod
    def is_actionable(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        return bool(
            re.search(
                r"(cve-\d{4}-\d+|ghsa-|rce|exploit|bypass|patch|fix|advisory|"
                r"incident|breach|credential|auth|ssrf|privesc|container escape)",
                text,
            )
        )

    @staticmethod
    def is_ai_heavy(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        has_ai = bool(re.search(r"\b(ai|llm|agentic|model|prompt)\b", text))
        has_cloud_security = bool(
            re.search(
                r"(cloud|kubern|k8s|container|iam|terraform|aws|azure|gcp|"
                r"serverless|envoy|postgres|clickhouse|redis|qemu|kvm|cve|ghsa)",
                text,
            )
        )
        return has_ai and not has_cloud_security

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _extract_domain(url: str) -> str:
        if not url:
            return ""
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return ""
        return netloc[4:] if netloc.startswith("www.") else netloc
