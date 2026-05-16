"""Deterministic quality gate checks for briefing drafts."""

from __future__ import annotations

from collections import Counter
import re

from bcn.briefing.text import canonical_url_key
from bcn.briefing.text import extract_raw_urls
from bcn.briefing.text import item_url_keys
from bcn.briefing.text import preferred_item_url
from bcn.common.config import Settings

_AI_STAMP_PATTERNS = (
    re.compile(
        r"clouds?\s+are\s+getting.+tools?\s+are\s+just\s+getting", re.IGNORECASE
    ),
    re.compile(r"\b(in\s+today'?s\s+(?:fast|rapidly)\s+evolving)\b", re.IGNORECASE),
    re.compile(r"\bever[-\s]evolving\b", re.IGNORECASE),
)
_RAW_MARKDOWN_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_BANNED_SECTION_TITLE = re.compile(
    r"(?im)^\*{0,2}\s*additional\s+high[-\s]signal\s+items\s*\*{0,2}\s*$"
)
_TRUNCATION_ARTIFACT = re.compile(r"\.\.\.\s*;")
_FALLBACK_PHRASE = re.compile(
    r"validate exposure and queue patch/detection checks\.?",
    re.IGNORECASE,
)
_DENSE_LINK_BLOCK = re.compile(r"(?m)\)\s*—[^\n]*\n\[[^\]]+\]\(https?://")
_ADJACENT_BOLD_HEADINGS = re.compile(r"(?m)^\*\*[^\n]+\*\*\n\*\*[^\n]+\*\*$")


class BriefingQualityGate:
    """Applies deterministic quality checks prior to publishing."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def char_limits(self, mode: str) -> tuple[int, int, int]:
        """Return per-mode markdown length targets."""
        if mode == "quiet_day":
            return (
                int(self.settings.briefing_quiet_day_min_chars),
                int(self.settings.briefing_quiet_day_target_chars),
                int(self.settings.briefing_quiet_day_hard_max_chars),
            )
        if mode == "monthly_newsletter":
            return (
                int(self.settings.briefing_monthly_min_chars),
                int(self.settings.briefing_monthly_target_chars),
                int(self.settings.briefing_monthly_hard_max_chars),
            )
        return (
            int(self.settings.briefing_min_chars),
            int(self.settings.briefing_target_chars),
            int(self.settings.briefing_hard_max_chars),
        )

    def evaluate(
        self,
        markdown: str,
        selected_items: list[dict],
        mode: str,
        min_chars: int,
        hard_max_chars: int,
    ) -> dict[str, object]:
        """Run deterministic checks before critic/model feedback."""
        hard_issues: list[str] = []
        soft_issues: list[str] = []
        gate_mode = (
            str(getattr(self.settings, "briefing_gate_mode", "balanced"))
            .strip()
            .lower()
        )
        gate_mode = (
            gate_mode if gate_mode in {"strict", "balanced", "minimal"} else "balanced"
        )
        strict_structure = gate_mode == "strict"
        check_structure = gate_mode != "minimal"
        body = (markdown or "").strip()
        length = len(body)

        if length < min_chars:
            hard_issues.append(
                f"Digest too short ({length} chars, need at least {min_chars})."
            )
        if length > hard_max_chars:
            hard_issues.append(
                f"Digest too long ({length} chars, hard max {hard_max_chars})."
            )

        if check_structure:
            heading_count = len(
                re.findall(r"^\*\*.+\*\*\s*$", body, flags=re.MULTILINE)
            )
            if len(selected_items) <= 1:
                min_sections = 1
            else:
                if mode == "quiet_day":
                    min_sections = 1
                elif mode == "monthly_newsletter":
                    min_sections = 4
                else:
                    min_sections = 2
            if heading_count < min_sections:
                issue = f"Too few sections ({heading_count}); expected at least {min_sections}."
                if strict_structure:
                    hard_issues.append(issue)
                else:
                    soft_issues.append(issue)

        selected_url_keys = {
            key for item in selected_items for key in item_url_keys(item)
        }

        url_counts: Counter[str] = Counter()
        unexpected_urls: list[str] = []
        seen_unexpected: set[str] = set()
        for raw_url in extract_raw_urls(body):
            key = canonical_url_key(raw_url)
            if key:
                url_counts[key] += 1
                if key not in selected_url_keys and key not in seen_unexpected:
                    seen_unexpected.add(key)
                    unexpected_urls.append(key)

        if unexpected_urls:
            hard_issues.append(
                "Unexpected URL not present in selected items: "
                + ", ".join(unexpected_urls[:3])
            )

        for item in selected_items:
            expected_keys = item_url_keys(item)
            if not expected_keys:
                continue
            count = sum(url_counts.get(key, 0) for key in expected_keys)
            expected = canonical_url_key(preferred_item_url(item))
            if count == 0:
                hard_issues.append(f"Missing selected URL: {expected or str(item.get('url', ''))}")
            elif count > 1:
                hard_issues.append(
                    f"Selected URL appears multiple times: {expected or str(item.get('url', ''))}"
                )

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if lines:
            tail = lines[-1]
            if re.fullmatch(r"\*\*.+\*\*", tail):
                soft_issues.append("Digest ends with an unfinished section header.")
            if tail.endswith(":") and len(tail) <= 120:
                soft_issues.append("Digest ends with unfinished lead-in text.")

        for pattern in _AI_STAMP_PATTERNS:
            if pattern.search(body):
                soft_issues.append("Contains repetitive AI-stamp phrasing.")
                break

        template_headings = re.findall(
            r"(?im)^\*\*(?:detection|source|threat|response|mitigation|intel)\s*:",
            body,
        )
        if len(template_headings) >= 2:
            soft_issues.append(
                "Section titles are repetitive template labels (e.g., Detection: ...)."
            )

        source_fields = re.findall(r"(?im)^\*?\s*source\s*:", body)
        if source_fields:
            soft_issues.append(
                "Standalone 'Source:' field lines detected; references should be inline."
            )

        if _RAW_MARKDOWN_HEADING.search(body):
            soft_issues.append(
                "Raw markdown heading markers (`#`) detected; normalize heading styling."
            )

        if _ADJACENT_BOLD_HEADINGS.search(body):
            soft_issues.append(
                "Adjacent section headings detected without body text between them."
            )

        if _DENSE_LINK_BLOCK.search(body):
            soft_issues.append(
                "Consecutive link items detected without blank-line spacing."
            )

        if _BANNED_SECTION_TITLE.search(body):
            soft_issues.append(
                "Generic section title detected (`Additional High-Signal Items`); use topical headings."
            )

        if _TRUNCATION_ARTIFACT.search(body):
            hard_issues.append(
                "Truncation artifact detected (`...;`) in briefing text."
            )

        if _FALLBACK_PHRASE.search(body):
            hard_issues.append("Fallback phrase artifact detected in briefing text.")

        headings = re.findall(r"(?m)^\*\*(.+?)\*\*\s*$", body)
        stems = [h.strip().split()[0].lower() for h in headings if h.strip()]
        repeated_stems = [stem for stem, count in Counter(stems).items() if count > 1]
        if repeated_stems:
            soft_issues.append(
                "Section headings repeat the same stem; diversify title phrasing."
            )

        source_counts = Counter(
            str(i.get("source_type", "")).lower() for i in selected_items
        )
        if source_counts:
            dominant = source_counts.most_common(1)[0][1]
            if dominant >= max(4, len(selected_items)):
                soft_issues.append(
                    "Source diversity is too narrow (single source dominates)."
                )

        issues = hard_issues + soft_issues
        return {
            "passed": len(hard_issues) == 0,
            "hard_issues": hard_issues[:16],
            "soft_issues": soft_issues[:16],
            "issues": issues[:16],
        }
