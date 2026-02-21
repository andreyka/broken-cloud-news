"""Deterministic quality gate checks for briefing drafts."""

from __future__ import annotations

import re
from collections import Counter

from bcn.config import Settings
from bcn.briefing.text import normalize_url

_AI_STAMP_PATTERNS = (
    re.compile(r"clouds?\s+are\s+getting.+tools?\s+are\s+just\s+getting", re.IGNORECASE),
    re.compile(r"\b(in\s+today'?s\s+(?:fast|rapidly)\s+evolving)\b", re.IGNORECASE),
    re.compile(r"\bever[-\s]evolving\b", re.IGNORECASE),
)


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
        body = (markdown or "").strip()
        length = len(body)

        if length < min_chars:
            hard_issues.append(f"Digest too short ({length} chars, need at least {min_chars}).")
        if length > hard_max_chars:
            hard_issues.append(f"Digest too long ({length} chars, hard max {hard_max_chars}).")

        heading_count = len(re.findall(r"^\*\*.+\*\*$", body, flags=re.MULTILINE))
        min_sections = 2 if mode == "quiet_day" else 3
        if heading_count < min_sections:
            hard_issues.append(
                f"Too few sections ({heading_count}); expected at least {min_sections}."
            )

        op_match = re.search(
            r"\*\*Operator Moves \(next 24h\)\*\*\s*(.*?)(?:\n\*\*|\Z)",
            body,
            flags=re.DOTALL,
        )
        if not op_match:
            hard_issues.append("Missing **Operator Moves (next 24h)** section.")
        else:
            op_bullets = [
                ln for ln in (line.strip() for line in op_match.group(1).splitlines())
                if ln.startswith("- ")
            ]
            if len(op_bullets) != 3:
                hard_issues.append("Operator Moves section must contain exactly 3 bullets.")

        url_counts: Counter[str] = Counter()
        for raw_url in re.findall(r"https?://[^\s)\]>]+", body):
            url_counts[normalize_url(raw_url)] += 1

        for item in selected_items:
            expected = normalize_url(str(item.get("url", "")))
            if not expected:
                continue
            count = url_counts.get(expected, 0)
            if count == 0:
                hard_issues.append(f"Missing selected URL: {expected}")
            elif count > 1:
                hard_issues.append(f"Selected URL appears multiple times: {expected}")

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
            soft_issues.append("Section titles are repetitive template labels (e.g., Detection: ...).")

        source_fields = re.findall(r"(?im)^\*?\s*source\s*:", body)
        if source_fields:
            soft_issues.append("Standalone 'Source:' field lines detected; references should be inline.")

        source_counts = Counter(str(i.get("source_type", "")).lower() for i in selected_items)
        if source_counts:
            dominant = source_counts.most_common(1)[0][1]
            if dominant >= max(4, len(selected_items)):
                soft_issues.append("Source diversity is too narrow (single source dominates).")

        issues = hard_issues + soft_issues
        return {
            "passed": len(hard_issues) == 0,
            "hard_issues": hard_issues[:16],
            "soft_issues": soft_issues[:16],
            "issues": issues[:16],
        }
