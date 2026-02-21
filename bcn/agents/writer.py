"""Writer agent: generates daily briefings with cover images."""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from typing_extensions import override

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from a2a.utils import new_agent_text_message

from bcn.comfyui import ComfyUIClient
from bcn.config import Settings
from bcn.db import (
    get_analyzed_items,
    get_recent_briefings,
    get_recent_published_items,
    insert_briefing,
)
from bcn.llm import LLMClient

logger = logging.getLogger(__name__)

_TRUSTED_RSS_DOMAINS = frozenset({
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
})

_AI_STAMP_PATTERNS = (
    re.compile(r"clouds?\s+are\s+getting.+tools?\s+are\s+just\s+getting", re.IGNORECASE),
    re.compile(r"\b(in\s+today'?s\s+(?:fast|rapidly)\s+evolving)\b", re.IGNORECASE),
    re.compile(r"\bever[-\s]evolving\b", re.IGNORECASE),
)

SKILLS = [
    AgentSkill(
        id="generate_briefing",
        name="Generate Briefing",
        description="Generate a security briefing with cover image from top-scored items",
        tags=["briefing", "writer"],
        examples=["write", "generate_briefing", "generate briefing"],
    ),
]


class WriterExecutor(AgentExecutor):
    """A2A agent that composes briefings from top-scored items."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
        )
        self.comfyui = ComfyUIClient(
            base_url=settings.comfyui_url,
            timeout=settings.comfyui_timeout,
            poll_interval=settings.comfyui_poll_interval,
        )

    @override
    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Generate a briefing from top-scored items and store it as DRAFT."""
        items = await get_analyzed_items(
            min_score=self.settings.relevance_threshold,
            hours=self.settings.briefing_lookback_hours,
        )

        if not items:
            msg = (
                f"Quiet day — no items scored >= {self.settings.relevance_threshold} "
                f"in the last {self.settings.briefing_lookback_hours}h. "
                f"Skipping briefing."
            )
            logger.info(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return

        item_dicts = [dict(i) for i in items]
        recent_published = await get_recent_published_items(
            hours=self.settings.briefing_novelty_lookback_hours,
            limit=self.settings.briefing_novelty_max_items,
        )
        quiet_mode = self._is_quiet_day(item_dicts)
        mode = "quiet_day" if quiet_mode else "standard"
        min_chars, target_chars, hard_max_chars = self._char_limits(mode)

        selected_items = self._select_items_for_briefing(
            item_dicts,
            recent_published=[dict(r) for r in recent_published],
            quiet_mode=quiet_mode,
        )
        if not selected_items:
            msg = "No items remained after quality/diversity filtering. Skipping briefing."
            logger.info(msg)
            event_queue.enqueue_event(new_agent_text_message(msg))
            return

        history = await get_recent_briefings(limit=self.settings.briefing_history_items)
        history_items = [dict(r) for r in history]

        briefing_body = await self.llm.generate_briefing(
            selected_items,
            recent_briefings=history_items,
            mode=mode,
        )
        briefing_body = await self._postprocess_briefing(
            briefing_body=briefing_body,
            selected_items=selected_items,
            mode=mode,
            min_chars=min_chars,
            target_chars=target_chars,
            hard_max_chars=hard_max_chars,
        )

        if self.settings.briefing_critique_enabled:
            rounds = max(1, self.settings.briefing_critique_max_rounds)
            for round_idx in range(1, rounds + 1):
                gate = self._quality_gate(
                    markdown=briefing_body,
                    selected_items=selected_items,
                    mode=mode,
                    min_chars=min_chars,
                    hard_max_chars=hard_max_chars,
                )
                critique = await self.llm.critique_briefing(
                    draft_markdown=briefing_body,
                    items=selected_items,
                    mode=mode,
                    gate_issues=gate["issues"],
                )
                gate_passed = bool(gate.get("passed", False))
                critic_passed = bool(critique.get("passed", False))
                if gate_passed and critic_passed:
                    logger.info(
                        "Briefing critique passed at round %d (score=%s)",
                        round_idx,
                        critique.get("score"),
                    )
                    break

                feedback: list[str] = []
                feedback.extend(gate.get("issues", []))
                feedback.extend([str(i) for i in critique.get("issues", [])])
                feedback.extend([str(r) for r in critique.get("recommendations", [])])
                logger.info(
                    "Briefing critique round %d failed (gate=%s critic=%s score=%s), regenerating",
                    round_idx,
                    gate_passed,
                    critic_passed,
                    critique.get("score"),
                )
                briefing_body = await self.llm.revise_briefing(
                    draft_markdown=briefing_body,
                    items=selected_items,
                    feedback=feedback,
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                )
                briefing_body = await self._postprocess_briefing(
                    briefing_body=briefing_body,
                    selected_items=selected_items,
                    mode=mode,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    hard_max_chars=hard_max_chars,
                )

        briefing_body = self._normalize_section_headings(briefing_body)
        # Final deterministic safety net before persistence/distribution.
        final_missing = self._missing_items_for_markdown(briefing_body, selected_items)
        if final_missing:
            logger.warning(
                "Final guard: appending %d missing selected URLs before publishing",
                len(final_missing),
            )
            briefing_body = self._append_missing_items_section(briefing_body, final_missing)

        logger.info("LLM briefing generated (%d chars)", len(briefing_body))

        topics = "\n".join(f"- {i['title']}: {i['summary']}" for i in selected_items)
        cover_prompt = await self.llm.generate_cover_prompt(topics)
        logger.info("Cover prompt: %s", cover_prompt[:100])

        cover_url = ""
        try:
            timestamp = int(time.time() * 1000)
            prefix = f"Digest_Cover_{timestamp}"
            cover_url = await self.comfyui.generate_image(cover_prompt, prefix)
            logger.info("Cover image: %s", cover_url)
        except Exception:
            logger.exception("Failed to generate cover image, continuing without it")

        markdown = self._format_markdown(briefing_body, cover_url)
        html = self._format_html(briefing_body, cover_url)

        item_ids = [i["id"] for i in selected_items]
        briefing_id = await insert_briefing(
            content_markdown=markdown,
            content_html=html,
            cover_image_url=cover_url,
            cover_image_prompt=cover_prompt,
            item_ids=item_ids,
        )

        msg = f"Briefing {briefing_id} created with {len(selected_items)} items"
        logger.info(msg)
        event_queue.enqueue_event(new_agent_text_message(msg))

    def _select_items_for_briefing(
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
                self._priority_score(i, recent_items),
                int(i.get("relevance_score", 0)),
                self._parse_timestamp(i.get("published_at")),
            ),
            reverse=True,
        )

        max_items = (
            min(self.settings.briefing_max_items, self.settings.briefing_quiet_day_max_items)
            if quiet_mode
            else self.settings.briefing_max_items
        )
        max_ai = self.settings.briefing_max_ai_items
        max_twitter = self.settings.briefing_max_twitter_items
        max_rss = self.settings.briefing_max_rss_items
        max_per_domain = self.settings.briefing_max_items_per_domain
        mix_targets = self._mix_targets(quiet_mode)

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

            if not self._passes_source_floor(item) and not relax_soft_limits:
                return False
            source = str(item.get("source_type", "")).lower()
            if source == "twitter" and source_counts[source] >= max_twitter and not relax_soft_limits:
                return False
            if source == "rss" and source_counts[source] >= max_rss:
                return False
            domain = self._extract_domain(str(item.get("url", "")))
            if domain and max_per_domain > 0 and domain_counts[domain] >= max_per_domain:
                return False
            if self._is_ai_heavy(item) and ai_count >= max_ai and not relax_soft_limits:
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
            bucket_counts[self._classify_bucket(item)] += 1
            if self._is_ai_heavy(item):
                ai_count += 1

        # Pass 0: enforce mix targets by bucket first when possible.
        for bucket, target in mix_targets.items():
            if target <= 0:
                continue
            while bucket_counts[bucket] < target and len(selected) < max_items:
                candidate = None
                for item in ranked:
                    if self._classify_bucket(item) != bucket:
                        continue
                    if not can_add(item):
                        continue
                    candidate = item
                    break
                if not candidate:
                    break
                add(candidate)

        # Pass 1: ensure representation from major sources when available.
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

        # Pass 2: prioritize actionable high-score items.
        for item in ranked:
            if len(selected) >= max_items:
                break
            if not self._is_actionable(item):
                continue
            if can_add(item):
                add(item)

        # Pass 3: fill remaining slots by score with guardrails.
        for item in ranked:
            if len(selected) >= max_items:
                break
            if can_add(item):
                add(item)

        # Pass 4: if constraints were too strict, relax only AI/Twitter limits.
        if len(selected) < max_items:
            for item in ranked:
                if len(selected) >= max_items:
                    break
                if can_add(item, relax_soft_limits=True):
                    add(item)

        # Pass 5: if we still have too few items, fully relax caps to avoid empty digests.
        if len(selected) < min(max_items, 3):
            for item in ranked:
                if len(selected) >= max_items:
                    break
                item_id = str(item.get("id"))
                if item_id in selected_ids:
                    continue
                add(item)

        return selected

    def _priority_score(
        self,
        item: dict,
        recent_published: list[dict] | None = None,
    ) -> float:
        """Composite priority from relevance, exploitability, trust, engagement, novelty."""
        relevance = float(int(item.get("relevance_score", 0)))
        score = relevance
        score += self._engagement_bonus(item)
        score += self._exploitability_bonus(item)
        score += self._source_trust_bonus(item)
        score -= self._novelty_penalty(item, recent_published or [])
        return score

    def _mix_targets(self, quiet_mode: bool) -> dict[str, int]:
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

    def _engagement_bonus(self, item: dict) -> float:
        """Compute capped source-specific social-proof bonus for ranking."""
        max_bonus = max(0.0, float(self.settings.briefing_social_proof_max_bonus))
        weight = max(0.0, float(self.settings.briefing_social_proof_weight))
        if max_bonus <= 0 or weight <= 0:
            return 0.0

        raw_score = self._engagement_raw_score(item)
        if raw_score <= 0:
            return 0.0

        bonus = math.log1p(raw_score) * weight
        return min(max_bonus, bonus)

    def _engagement_raw_score(self, item: dict) -> float:
        """Extract source-specific engagement magnitude from raw payload."""
        source = str(item.get("source_type", "")).lower()
        raw = self._to_dict(item.get("raw_data"))

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

    def _exploitability_bonus(self, item: dict) -> float:
        """Boost concrete exploit/patch items over generic commentary."""
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        score = 0.0
        if re.search(r"(cve-\d{4}-\d+|ghsa-)", text):
            score += 1.35
        if re.search(r"(actively exploited|in the wild|zero[-\s]?day|rce|auth bypass|ssrf|privesc|container escape)", text):
            score += 0.95
        if re.search(r"(patch|fixed|mitigation|detection|ioc|rule|playbook)", text):
            score += 0.55
        return min(2.5, score)

    def _source_trust_bonus(self, item: dict) -> float:
        """Prefer durable primary sources while keeping social discovery."""
        source = str(item.get("source_type", "")).lower()
        if source == "ghsa":
            return 1.5
        if source == "rss":
            return 0.8 if self._is_trusted_rss_item(item) else 0.0
        if source == "reddit":
            return 0.25
        if source == "twitter":
            return 0.2
        return 0.0

    def _novelty_penalty(self, item: dict, recent_published: list[dict]) -> float:
        """Penalize near-duplicate stories from recent distributed digests."""
        if not recent_published:
            return 0.0

        cur_url = self._normalize_url(str(item.get("url", "")))
        cur_title = self._normalize_title(str(item.get("title", "")))
        best_title_similarity = 0.0
        same_url_seen = False

        for prev in recent_published:
            prev_url = self._normalize_url(str(prev.get("url", "")))
            if cur_url and prev_url and cur_url == prev_url:
                same_url_seen = True
                best_title_similarity = 1.0
                break

            prev_title = self._normalize_title(str(prev.get("title", "")))
            if not cur_title or not prev_title:
                continue
            sim = difflib.SequenceMatcher(None, cur_title, prev_title).ratio()
            if sim > best_title_similarity:
                best_title_similarity = sim

        if same_url_seen:
            return 3.0

        threshold = float(self.settings.briefing_novelty_title_similarity_threshold)
        if best_title_similarity < threshold:
            return 0.0

        span = max(0.01, 1.0 - threshold)
        scaled = (best_title_similarity - threshold) / span
        return min(2.3, 0.7 + (scaled * 1.6))

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize titles before similarity checks."""
        title = title.lower().strip()
        title = re.sub(r"https?://\S+", "", title)
        title = re.sub(r"[^a-z0-9\s\-_/.:]", " ", title)
        return re.sub(r"\s+", " ", title).strip()

    def _classify_bucket(self, item: dict) -> str:
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

    def _passes_source_floor(self, item: dict) -> bool:
        """Drop weak social/untrusted items unless relevance is very high."""
        source = str(item.get("source_type", "")).lower()
        relevance = int(item.get("relevance_score", 0))
        if relevance >= int(self.settings.briefing_social_floor_exempt_relevance):
            return True

        if source == "reddit":
            return self._engagement_raw_score(item) >= float(
                self.settings.briefing_min_reddit_engagement_score
            )

        if source == "twitter":
            return self._engagement_raw_score(item) >= float(
                self.settings.briefing_min_twitter_engagement_score
            )

        if source == "rss" and not self._is_trusted_rss_item(item):
            return relevance >= int(self.settings.briefing_untrusted_rss_min_score)

        return True

    def _is_trusted_rss_item(self, item: dict) -> bool:
        """Determine whether RSS item comes from a trusted source domain."""
        raw = self._to_dict(item.get("raw_data"))
        url_domain = self._extract_domain(str(item.get("url", "")))
        feed_domain = self._extract_domain(str(raw.get("feed_url", "")))
        for domain in (url_domain, feed_domain):
            if not domain:
                continue
            if any(domain == d or domain.endswith(f".{d}") for d in _TRUSTED_RSS_DOMAINS):
                return True
        return False

    def _is_quiet_day(self, items: list[dict]) -> bool:
        """Detect low-signal days and switch to deeper fewer-item mode."""
        if not self.settings.briefing_quiet_day_enabled:
            return False

        threshold = int(self.settings.briefing_quiet_day_high_signal_threshold)
        min_items = int(self.settings.briefing_quiet_day_min_high_signal_items)
        high_signal = 0
        for item in items:
            relevance = int(item.get("relevance_score", 0))
            if relevance < threshold:
                continue
            if self._is_actionable(item) or str(item.get("source_type", "")).lower() == "ghsa":
                high_signal += 1
        return high_signal < min_items

    def _char_limits(self, mode: str) -> tuple[int, int, int]:
        """Return per-mode markdown length targets."""
        if mode == "quiet_day":
            return (
                int(self.settings.briefing_quiet_day_min_chars),
                int(self.settings.briefing_quiet_day_target_chars),
                int(self.settings.briefing_quiet_day_hard_max_chars),
            )
        return (
            int(self.settings.briefing_min_chars),
            int(self.settings.briefing_target_chars),
            int(self.settings.briefing_hard_max_chars),
        )

    async def _postprocess_briefing(
        self,
        briefing_body: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        target_chars: int,
        hard_max_chars: int,
    ) -> str:
        """Enforce URL coverage and depth/length constraints on LLM draft."""
        markdown = self._normalize_section_headings(
            self._dedupe_markdown_links((briefing_body or "").strip())
        )

        for _ in range(2):
            missing_items = self._missing_items_for_markdown(markdown, selected_items)
            too_short = len(markdown) < min_chars
            if not missing_items and not too_short:
                break

            missing_urls = [str(i.get("url", "")) for i in missing_items if i.get("url")]
            markdown = await self.llm.enrich_briefing(
                draft_markdown=markdown,
                items=selected_items,
                min_chars=min_chars,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
                missing_urls=missing_urls or None,
                mode=mode,
            )
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )

        missing_items = self._missing_items_for_markdown(markdown, selected_items)
        if missing_items:
            markdown = self._append_missing_items_section(markdown, missing_items)

        if len(markdown) > hard_max_chars:
            markdown = await self.llm.tighten_briefing(
                markdown=markdown,
                target_chars=target_chars,
                hard_max_chars=hard_max_chars,
            )
            markdown = self._normalize_section_headings(
                self._dedupe_markdown_links(markdown.strip())
            )
            missing_items = self._missing_items_for_markdown(markdown, selected_items)
            if missing_items:
                markdown = self._append_missing_items_section(markdown, missing_items)

        if len(markdown) > hard_max_chars:
            markdown = self._clip_markdown(markdown, hard_max_chars)

        # URL coverage is a hard requirement; restore any missing links even if over target length.
        missing_items = self._missing_items_for_markdown(markdown, selected_items)
        if missing_items:
            markdown = self._append_missing_items_section(markdown, missing_items)

        return markdown.strip()

    @staticmethod
    def _normalize_section_headings(markdown: str) -> str:
        """Convert markdown headings to Telegram-friendly bold section lines."""
        lines = markdown.splitlines()
        normalized: list[str] = []
        for line in lines:
            match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
            if not match:
                normalized.append(line)
                continue
            heading = match.group(1).strip()
            heading = re.sub(r"^\*\*(.+)\*\*$", r"\1", heading).strip()
            normalized.append(f"**{heading}**")
        return "\n".join(normalized)

    def _quality_gate(
        self,
        markdown: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        """Run deterministic checks before asking the critic model."""
        issues: list[str] = []
        body = (markdown or "").strip()
        length = len(body)

        if length < min_chars:
            issues.append(f"Digest too short ({length} chars, need at least {min_chars}).")
        if length > hard_max_chars:
            issues.append(f"Digest too long ({length} chars, hard max {hard_max_chars}).")

        heading_count = len(re.findall(r"^\*\*.+\*\*$", body, flags=re.MULTILINE))
        min_sections = 2 if mode == "quiet_day" else 3
        if heading_count < min_sections:
            issues.append(
                f"Too few sections ({heading_count}); expected at least {min_sections}."
            )

        op_match = re.search(
            r"\*\*Operator Moves \(next 24h\)\*\*\s*(.*?)(?:\n\*\*|\Z)",
            body,
            flags=re.DOTALL,
        )
        if not op_match:
            issues.append("Missing **Operator Moves (next 24h)** section.")
        else:
            op_bullets = [
                ln for ln in (line.strip() for line in op_match.group(1).splitlines())
                if ln.startswith("- ")
            ]
            if len(op_bullets) != 3:
                issues.append("Operator Moves section must contain exactly 3 bullets.")

        url_counts: Counter[str] = Counter()
        for raw_url in re.findall(r"https?://[^\s)\]>]+", body):
            url_counts[self._normalize_url(raw_url)] += 1

        for item in selected_items:
            expected = self._normalize_url(str(item.get("url", "")))
            if not expected:
                continue
            count = url_counts.get(expected, 0)
            if count == 0:
                issues.append(f"Missing selected URL: {expected}")
            elif count > 1:
                issues.append(f"Selected URL appears multiple times: {expected}")

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if lines:
            tail = lines[-1]
            if re.fullmatch(r"\*\*.+\*\*", tail):
                issues.append("Digest ends with an unfinished section header.")
            if tail.endswith(":") and len(tail) <= 120:
                issues.append("Digest ends with unfinished lead-in text.")

        for pattern in _AI_STAMP_PATTERNS:
            if pattern.search(body):
                issues.append("Contains repetitive AI-stamp phrasing.")
                break

        source_counts = Counter(str(i.get("source_type", "")).lower() for i in selected_items)
        if source_counts:
            dominant = source_counts.most_common(1)[0][1]
            if dominant >= max(4, len(selected_items)):
                issues.append("Source diversity is too narrow (single source dominates).")

        return {
            "passed": len(issues) == 0,
            "issues": issues[:16],
        }

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        """Parse flexible timestamp values from DB records for sorting."""
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_actionable(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        return bool(re.search(
            r"(cve-\d{4}-\d+|ghsa-|rce|exploit|bypass|patch|fix|advisory|"
            r"incident|breach|credential|auth|ssrf|privesc|container escape)",
            text,
        ))

    @staticmethod
    def _is_ai_heavy(item: dict) -> bool:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        has_ai = bool(re.search(r"\b(ai|llm|agentic|model|prompt)\b", text))
        has_cloud_security = bool(re.search(
            r"(cloud|kubern|k8s|container|iam|terraform|aws|azure|gcp|"
            r"serverless|envoy|postgres|clickhouse|redis|qemu|kvm|cve|ghsa)",
            text,
        ))
        return has_ai and not has_cloud_security

    @staticmethod
    def _dedupe_markdown_links(markdown: str) -> str:
        """Remove duplicate links while keeping readable labels."""
        seen: set[str] = set()

        def repl(match: re.Match[str]) -> str:
            label = match.group(1)
            url = match.group(2)
            if url in seen:
                return label
            seen.add(url)
            return match.group(0)

        return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", repl, markdown)

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract normalized domain (no www) for diversity balancing."""
        if not url:
            return ""
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return ""
        return netloc[4:] if netloc.startswith("www.") else netloc

    @staticmethod
    def _extract_urls(markdown: str) -> set[str]:
        """Extract normalized HTTP(S) URLs from markdown/plain text."""
        raw_urls = re.findall(r"https?://[^\s)\]>]+", markdown)
        return {WriterExecutor._normalize_url(u) for u in raw_urls if u}

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URLs for robust inclusion checks."""
        trimmed = url.strip().rstrip(").,;!?")
        try:
            parsed = urlparse(trimmed)
        except Exception:
            return trimmed
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{scheme}://{netloc}{path}{query}"

    @staticmethod
    def _to_dict(raw_data: object) -> dict:
        """Safely coerce DB raw_data payload to dict."""
        if isinstance(raw_data, dict):
            return raw_data
        if isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _missing_items_for_markdown(cls, markdown: str, items: list[dict]) -> list[dict]:
        """Return selected items whose main URLs are missing from markdown."""
        present_urls = cls._extract_urls(markdown)
        missing: list[dict] = []
        for item in items:
            url = cls._normalize_url(str(item.get("url", "")))
            if url and url not in present_urls:
                missing.append(item)
        return missing

    @staticmethod
    def _append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
        """Append deterministic fallback lines for missing item URLs."""
        if not missing_items:
            return markdown

        lines = ["**Additional High-Signal Items**"]
        for item in missing_items:
            title = str(item.get("title") or "Untitled item").strip()
            summary = str(item.get("summary") or "").strip()
            summary = re.sub(r"\s+", " ", summary)
            if len(summary) > 180:
                summary = summary[:177].rstrip() + "..."
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if summary:
                lines.append(
                    f"- [{title}]({url}) — {summary}; validate exposure and queue patch/detection checks."
                )
            else:
                lines.append(
                    f"- [{title}]({url}) — validate exposure and queue patch/detection checks."
                )

        if len(lines) == 1:
            return markdown
        return markdown.rstrip() + "\n\n" + "\n".join(lines)

    @staticmethod
    def _clip_markdown(markdown: str, limit: int) -> str:
        """Hard-cap markdown length, preferring paragraph boundaries."""
        if len(markdown) <= limit:
            return markdown
        split_at = markdown.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = markdown.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        return markdown[:split_at].rstrip()

    @override
    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Cancel is not supported."""
        raise NotImplementedError("cancel not supported")

    @staticmethod
    def _format_markdown(briefing_body: str, cover_url: str) -> str:
        """Wrap the briefing body with an optional cover image in Markdown.

        Args:
            briefing_body: The LLM-generated briefing text.
            cover_url: URL of the cover image (may be empty).

        Returns:
            Complete Markdown document.
        """
        md = ""
        if cover_url:
            md += f"![Daily Cover]({cover_url})\n\n"
        md += briefing_body
        return md

    @staticmethod
    def _format_html(briefing_body: str, cover_url: str) -> str:
        """Convert the briefing body to basic HTML.

        Handles ``###``/``##`` headers, bold, italic, links, and paragraphs.

        Args:
            briefing_body: The LLM-generated briefing text.
            cover_url: URL of the cover image (may be empty).

        Returns:
            A minimal HTML document string.
        """
        html_body = briefing_body
        html_body = re.sub(
            r"^### (.+)$", r"<h3>\1</h3>", html_body, flags=re.MULTILINE
        )
        html_body = re.sub(
            r"^## (.+)$", r"<h2>\1</h2>", html_body, flags=re.MULTILINE
        )
        html_body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html_body)
        html_body = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html_body)
        html_body = re.sub(
            r"\[([^\]]+)]\(([^)]+)\)", r'<a href="\2">\1</a>', html_body
        )
        html_body = re.sub(r"\n{2,}", "</p>\n<p>", html_body)
        html_body = f"<p>{html_body}</p>"

        parts = ["<html><body>"]
        if cover_url:
            parts.append(
                f'<img src="{cover_url}" alt="Daily Cover" '
                f'style="max-width:600px"/>'
            )
        parts.append(html_body)
        parts.append("</body></html>")
        return "\n".join(parts)
