"""Qwen LLM client for item analysis and briefing generation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from bcn.models import AnalysisResult

logger = logging.getLogger(__name__)

# Analyzer system prompt
ANALYZER_SYSTEM_PROMPT = (
    "You are a principal cloud security engineer curating the 'Broken Cloud' digest.\n"
    "Goal: prioritize practical, patchable, high-signal cloud security items.\n\n"
    "Audience:\n"
    "- Cloud provider defenders (CSP side)\n"
    "- Cloud customers running production workloads\n\n"
    "Scoring rules (1-10):\n"
    "- 9-10: actively exploited / high-confidence exploit path / concrete patch+detection guidance.\n"
    "- 7-8: major cloud-native security issue with actionable technical detail.\n"
    "- 5-6: relevant but weakly actionable or missing technical depth.\n"
    "- 1-4: noisy, generic AI hype, CTF/event announcements, marketing, off-topic items.\n\n"
    "Hard scope: cloud and cloud-native security only (Kubernetes, containers, cloud IAM, "
    "serverless, AWS/Azure/GCP, Terraform, supply chain, managed data services, service mesh).\n\n"
    "Output STRICT JSON only:\n"
    "{\n"
    '  "summary": "1-2 sentence practitioner-focused summary",\n'
    '  "relevance_score": 1-10,\n'
    '  "tags": ["3-5 technical tags"],\n'
    '  "image_prompt": "dramatic visual concept, no text"\n'
    "}\n\n"
    "Think through relevance and actionability internally before answering; "
    "do not reveal reasoning or intermediate steps.\n\n"
    "Few-shot examples:\n"
    "Example A (high signal):\n"
    "Input title: Critical auth bypass in managed Kubernetes ingress allows unauthenticated admin actions\n"
    "Input content: includes exploit path, affected versions, patch release and detection query.\n"
    "Output JSON:\n"
    '{"summary":"Auth bypass in managed K8s ingress enables unauthenticated admin actions; patch immediately and hunt for suspicious admin API calls.","relevance_score":9,"tags":["kubernetes","auth-bypass","managed-k8s","ingress","detection"],"image_prompt":"storm over a neon container cluster, breached gateway shield, high contrast cinematic lighting"}\n\n'
    "Example B (low signal):\n"
    "Input title: Join our weekend CTF challenge about AI agents!\n"
    "Input content: event announcement, no production exploit or remediation.\n"
    "Output JSON:\n"
    '{"summary":"CTF/event announcement without production cloud impact or actionable remediation guidance.","relevance_score":2,"tags":["ctf","event","ai-agents"],"image_prompt":"digital arena with training targets and holographic puzzles, dramatic but abstract"}'
)

# Cover art system prompt
COVER_ART_SYSTEM_PROMPT = (
    "You are an AI art director for a cloud security newsletter. Create one dramatic, "
    "high-contrast image prompt that visualizes the provided topics. No text in image."
)

BRIEFING_SYSTEM_PROMPT = (
    "You are the technical editor of 'Broken Cloud Daily Briefing'.\n"
    "Write like a senior cloud security engineer + tech writer: human, practical, vivid, and precise.\n\n"
    "Primary objective:\n"
    "- Deliver actionable signal for offensive and defensive practitioners.\n"
    "- Prefer exploitable flaws, incidents, hardening changes, patches, and detection opportunities.\n"
    "- De-prioritize generic AI hype and social chatter.\n\n"
    "Format rules:\n"
    "1. Markdown only.\n"
    "2. Opening: 1-2 lines with a concrete scene setter (no cliches).\n"
    "3. 1-4 short themed sections (`**Section Name**`) depending on signal volume.\n"
    "4. Cover every candidate item exactly once with its main Markdown link.\n"
    "5. Each item line must include: what happened, why it matters, and the immediate move.\n"
    "6. Keep an organic narrative flow; do not use template labels like `Detection:` or `Source:`.\n"
    "7. Integrate attribution links inline, not as standalone `Source:` fields.\n"
    "8. Add operator next steps only when useful; use compact bullets if needed, but they are optional.\n"
    "9. Use concrete section names; do not use placeholders like `Additional High-Signal Items`.\n"
    "10. Separate major paragraphs/items with blank lines for readability.\n"
    "11. One short closing line.\n\n"
    "Hard constraints:\n"
    "- Every candidate item URL must appear exactly once in the output.\n"
    "- Use only URLs provided in input.\n"
    "- Never duplicate the same URL in the same item line.\n"
    "- Never invent links.\n"
    "- Avoid repetitive cliches and AI-stamp phrasing.\n"
    "- Avoid repeating section title stems across sections.\n"
    "- Never use repeated field scaffolding like `Detection:` / `Source:`.\n"
    "- Never output truncation artifacts like `...;`.\n"
    "- Never include fallback filler like `validate exposure and queue patch/detection checks`.\n"
    "- Target 1200-2300 characters.\n\n"
    "Think internally about novelty and phrasing diversity using the provided history, "
    "but do not output reasoning.\n\n"
    "Few-shot style example (target quality):\n"
    "Opening: 'Patch Tuesday energy, but for cloud control planes.'\n"
    "Section: **Identity Breaks**\n"
    "Line: [Managed IAM token confusion bug](https://example.com/advisory) enables cross-tenant privilege use; ship vendor patch and alert on anomalous token audience mismatches.\n"
    "Section: **Control Plane Heat**\n"
    "Line: [Ingress auth bypass in K8s](https://example.com/k8s) exposes admin verbs without auth; prioritize cluster edge upgrades and tighten admission policies.\n"
    "Closing: 'Patch first, postmortem later.'"
)

BRIEFING_STORY_CARD_PROMPT = (
    "You are preparing story cards for a cloud-security daily digest.\n"
    "For each input item, output one JSON object with these fields:\n"
    '- "main_url": exact item URL,\n'
    '- "what_happened": concrete incident/vulnerability change,\n'
    '- "why_now": urgency and timing,\n'
    '- "who_impacted": affected operators/users,\n'
    '- "offensive_angle": realistic attacker path,\n'
    '- "defensive_action_24h": immediate defensive step,\n'
    '- "reference_links": list of up to 3 provided links only.\n'
    "Rules:\n"
    "- Use only facts from provided items and links.\n"
    "- Do not invent claims or URLs.\n"
    "- Keep language specific and practitioner-oriented.\n"
    "Return STRICT JSON only:\n"
    "{\n"
    '  "cards": [\n'
    "    {\n"
    '      "main_url": "https://...",\n'
    '      "what_happened": "...",\n'
    '      "why_now": "...",\n'
    '      "who_impacted": "...",\n'
    '      "offensive_angle": "...",\n'
    '      "defensive_action_24h": "...",\n'
    '      "reference_links": ["https://..."]\n'
    "    }\n"
    "  ]\n"
    "}"
)

BRIEFING_TIGHTENER_PROMPT = (
    "You are editing a cloud-security digest for Telegram caption delivery.\n"
    "Rewrite the text to fit the target length while preserving meaning and links.\n"
    "Rules: keep Markdown links intact, remove fluff, keep practical details, keep section structure, "
    "and output only revised Markdown."
)

BRIEFING_ENRICHER_PROMPT = (
    "You are rewriting a cloud-security digest draft.\n"
    "Goal: increase depth, practical value, and stylistic freshness while preserving factual grounding.\n"
    "Rules:\n"
    "- Keep Markdown format.\n"
    "- Keep all links valid and from the provided item list only.\n"
    "- Include every required URL exactly once.\n"
    "- Keep sections compact but informative, with concrete defensive/offensive next steps.\n"
    "- Keep prose organic; avoid template fields (`Detection:`, `Source:`) and rigid label repetition.\n"
    "- Attribution links should be woven into sentence flow, not separate source lines.\n"
    "- Avoid generic AI hype language and stale newsletter cliches.\n"
    "- Avoid placeholder section names (e.g., `Additional High-Signal Items`).\n"
    "- Remove truncation artifacts like `...;` and legacy fallback filler phrases.\n"
    "- Keep clear spacing between major items/paragraphs.\n"
    "- Stay within requested length bounds.\n"
    "Output only the rewritten digest."
)

BRIEFING_CRITIC_PROMPT = (
    "You are the editorial quality gate for 'Broken Cloud Daily Briefing'.\n"
    "Judge drafts like a demanding cloud-security staff engineer.\n\n"
    "Input includes deterministic findings split as:\n"
    "- HARD issues: objective must-fix constraints (treat as blocking)\n"
    "- SOFT issues: heuristic signals (use judgment; do not fail purely on heuristics)\n\n"
    "Evaluate:\n"
    "- Actionability (clear immediate moves for defenders/operators)\n"
    "- Practicality (patch/detect/contain relevance, not generic commentary)\n"
    "- Diversity (no monoculture of source/theme)\n"
    "- Clarity and flow (no abrupt/mid-thought sections)\n"
    "- Style quality (avoid AI-cliche and repetitive framing)\n"
    "- Organic writing (no rigid template headers or standalone source fields)\n"
    "- Link hygiene (selected URLs covered exactly once)\n\n"
    "Return STRICT JSON only:\n"
    "{\n"
    '  "passed": true|false,\n'
    '  "score": 0-100,\n'
    '  "dimension_scores": {\n'
    '    "actionability": 0-100,\n'
    '    "source_diversity": 0-100,\n'
    '    "link_hygiene": 0-100,\n'
    '    "clarity": 0-100,\n'
    '    "style": 0-100\n'
    "  },\n"
    '  "issues": ["short concrete issue 1", "issue 2"],\n'
    '  "recommendations": ["short concrete fix 1", "fix 2"]\n'
    "}\n"
)

BRIEFING_REWRITE_PROMPT = (
    "You are rewriting a cloud-security digest to satisfy an editorial critic.\n"
    "Apply all feedback while keeping factual grounding and valid links.\n"
    "Rules:\n"
    "- Markdown only.\n"
    "- Keep all selected URLs present exactly once.\n"
    "- Improve actionability, clarity, and section flow.\n"
    "- Remove template field patterns (`Detection:`, `Source:`) and repetitive heading formulas.\n"
    "- Keep references inline in natural prose.\n"
    "- Remove repetitive/boilerplate phrasing.\n"
    "- Replace placeholder section names with concrete topical titles.\n"
    "- Remove truncation artifacts like `...;` and fallback filler phrases.\n"
    "- Ensure readable spacing between major items.\n"
    "- Stay inside requested length bounds.\n"
    "Output only revised digest."
)

BRIEFING_FACT_VERIFIER_PROMPT = (
    "You are a factual verifier for a cloud-security briefing.\n"
    "Validate factual grounding against provided source item summaries and links.\n"
    "Flag ungrounded claims, factual overreach, and top-story prioritization mistakes.\n"
    "Treat event/CTF announcements as top story as a hard failure.\n"
    "Return STRICT JSON only:\n"
    "{\n"
    '  "passed": true|false,\n'
    '  "score": 0-100,\n'
    '  "hard_issues": ["blocking issue"],\n'
    '  "soft_issues": ["non-blocking issue"],\n'
    '  "recommendations": ["fix action"]\n'
    "}\n"
)

_SKIP_DOMAINS = frozenset({
    "nvd.nist.gov", "cve.mitre.org", "cve.org", "access.redhat.com",
})


class LLMClient:
    """OpenAI-compatible chat client for the Qwen LLM on DGX Spark."""

    def __init__(self, base_url: str, model: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)
        self._url_status_cache: dict[str, bool] = {}

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        retries: int = 3,
    ) -> str:
        """Send a chat-completion request with automatic retries."""
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "LLM request failed (attempt %d/%d, %s), retrying in %ds",
                        attempt, retries, type(exc).__name__, wait,
                    )
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def analyze_item(self, title: str, content: str) -> AnalysisResult:
        """Score and summarize a single news item."""
        user_msg = f"Title: {title}\n\nContent: {content}"
        raw = await self.chat(ANALYZER_SYSTEM_PROMPT, user_msg)

        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            return AnalysisResult(
                summary=parsed.get("summary", raw[:500]),
                relevance_score=max(1, min(10, int(parsed.get("relevance_score", 5)))),
                tags=parsed.get("tags", []),
                image_prompt=parsed.get("image_prompt", "cloud security concept art"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("Failed to parse LLM JSON, using fallback")
            return AnalysisResult(
                summary=raw[:500],
                relevance_score=5,
                tags=[],
                image_prompt="cloud security concept art",
            )

    async def generate_briefing(
        self,
        items: list[dict],
        recent_briefings: list[dict] | None = None,
        mode: str = "standard",
    ) -> str:
        """Generate an editorial briefing from story cards derived from items."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))

        story_cards = await self._build_story_cards(entries, items)
        cards_json = json.dumps({"cards": story_cards}, ensure_ascii=False, indent=2)
        style_memory = self._build_style_memory(recent_briefings or [])
        mode_block = (
            "Mode: quiet_day.\n"
            "- Fewer stories are acceptable.\n"
            "- Add deeper operator guidance and telemetry checks per item.\n"
            "- Explain tradeoffs clearly.\n"
            if mode == "quiet_day"
            else "Mode: standard daily briefing."
        )
        user_msg = (
            f"{mode_block}\n\nStory cards ({len(story_cards)} total). "
            "Use every `main_url` exactly once in the final briefing.\n\n"
            + cards_json
        )
        if style_memory:
            user_msg += "\n\nRecent briefing patterns to avoid repeating:\n" + style_memory
        return await self.chat(BRIEFING_SYSTEM_PROMPT, user_msg)

    async def tighten_briefing(
        self,
        markdown: str,
        target_chars: int = 900,
        hard_max_chars: int = 1000,
    ) -> str:
        """Compress briefing text while preserving links and practical meaning."""
        user_msg = (
            f"Target length: <= {target_chars} chars (hard max {hard_max_chars}).\n\n"
            f"Digest:\n{markdown}"
        )
        tightened = await self.chat(BRIEFING_TIGHTENER_PROMPT, user_msg)
        return tightened.strip()

    async def enrich_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        min_chars: int = 1200,
        target_chars: int = 1700,
        hard_max_chars: int = 2300,
        missing_urls: list[str] | None = None,
        mode: str = "standard",
    ) -> str:
        """Rewrite a draft briefing to improve depth and ensure URL coverage."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))
        story_cards = await self._build_story_cards(entries, items)
        cards_json = json.dumps({"cards": story_cards}, ensure_ascii=False, indent=2)

        mode_hint = (
            "quiet_day: deepen practical guidance per item and include concrete playbook flavor."
            if mode == "quiet_day"
            else "standard: balanced actionable digest."
        )
        user_msg = (
            f"Length goal: {min_chars}-{hard_max_chars} chars (target ~{target_chars}).\n"
            f"Mode: {mode_hint}\n"
            f"Current draft length: {len(draft_markdown)} chars.\n\n"
            f"Current draft:\n{draft_markdown}\n\n"
            f"Story cards ({len(story_cards)} total):\n\n"
            + cards_json
        )
        if missing_urls:
            missing = "\n".join(f"- {u}" for u in missing_urls)
            user_msg += "\n\nRequired URLs currently missing and must be included exactly once:\n" + missing

        rewritten = await self.chat(BRIEFING_ENRICHER_PROMPT, user_msg)
        return rewritten.strip()

    async def critique_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        mode: str = "standard",
        gate_hard_issues: list[str] | None = None,
        gate_soft_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Critique a draft briefing and return structured pass/fail guidance."""
        item_lines = [
            f"- [{item.get('source_type', '')}] {item.get('title', '')} :: {item.get('url', '')}"
            for item in items
        ]
        hard_text = "\n".join(f"- {issue}" for issue in (gate_hard_issues or [])) or "- none"
        soft_text = "\n".join(f"- {issue}" for issue in (gate_soft_issues or [])) or "- none"
        mode_text = "quiet_day" if mode == "quiet_day" else "standard"
        user_msg = (
            f"Mode: {mode_text}\n"
            f"Selected items ({len(items)}):\n"
            + "\n".join(item_lines)
            + "\n\nLocal quality-gate findings (HARD):\n"
            + hard_text
            + "\n\nLocal quality-gate findings (SOFT):\n"
            + soft_text
            + "\n\nDraft:\n"
            + draft_markdown
        )
        raw = await self.chat(BRIEFING_CRITIC_PROMPT, user_msg)
        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            passed = bool(parsed.get("passed", False))
            score = max(0, min(100, int(parsed.get("score", 0))))
            dimension_scores = parsed.get("dimension_scores", {}) or {}
            if not isinstance(dimension_scores, dict):
                dimension_scores = {}
            dims = {
                "actionability": max(0, min(100, int(dimension_scores.get("actionability", score)))),
                "source_diversity": max(0, min(100, int(dimension_scores.get("source_diversity", score)))),
                "link_hygiene": max(0, min(100, int(dimension_scores.get("link_hygiene", score)))),
                "clarity": max(0, min(100, int(dimension_scores.get("clarity", score)))),
                "style": max(0, min(100, int(dimension_scores.get("style", score)))),
            }
            issues = parsed.get("issues", [])
            recommendations = parsed.get("recommendations", [])
            if not isinstance(issues, list):
                issues = [str(issues)]
            if not isinstance(recommendations, list):
                recommendations = [str(recommendations)]
            return {
                "passed": passed,
                "score": score,
                "dimension_scores": dims,
                "issues": [str(i) for i in issues[:12]],
                "recommendations": [str(r) for r in recommendations[:12]],
            }
        except Exception:
            logger.warning("Failed to parse briefing critique JSON, using fallback")
            return {
                "passed": False,
                "score": 0,
                "dimension_scores": {
                    "actionability": 0,
                    "source_diversity": 0,
                    "link_hygiene": 0,
                    "clarity": 0,
                    "style": 0,
                },
                "issues": ["Critic response parsing failed"],
                "recommendations": ["Regenerate briefing with stricter structure and actionable guidance"],
            }

    async def verify_briefing_facts(
        self,
        draft_markdown: str,
        items: list[dict],
        *,
        mode: str = "standard",
        deterministic_issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Verify factual grounding of a draft against provided items."""
        item_lines = [
            (
                f"- [{item.get('source_type', '')}] {item.get('title', '')}\n"
                f"  URL: {item.get('url', '')}\n"
                f"  Summary: {item.get('summary', '')}"
            )
            for item in items
        ]
        mode_text = "quiet_day" if mode == "quiet_day" else "standard"
        det = "\n".join(f"- {line}" for line in (deterministic_issues or [])) or "- none"
        user_msg = (
            f"Mode: {mode_text}\n\n"
            f"Selected items ({len(items)}):\n"
            + "\n".join(item_lines)
            + "\n\nDeterministic verifier findings:\n"
            + det
            + "\n\nDraft:\n"
            + draft_markdown
        )
        raw = await self.chat(BRIEFING_FACT_VERIFIER_PROMPT, user_msg)
        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            hard = parsed.get("hard_issues", [])
            soft = parsed.get("soft_issues", [])
            recs = parsed.get("recommendations", [])
            if not isinstance(hard, list):
                hard = [str(hard)]
            if not isinstance(soft, list):
                soft = [str(soft)]
            if not isinstance(recs, list):
                recs = [str(recs)]
            return {
                "passed": bool(parsed.get("passed", False)),
                "score": max(0, min(100, int(parsed.get("score", 0)))),
                "hard_issues": [str(i) for i in hard[:12]],
                "soft_issues": [str(i) for i in soft[:12]],
                "recommendations": [str(i) for i in recs[:12]],
            }
        except Exception:
            logger.warning("Failed to parse verifier JSON, using fallback")
            return {
                "passed": False,
                "score": 0,
                "hard_issues": ["Verifier response parsing failed"],
                "soft_issues": [],
                "recommendations": ["Re-run factual verification and tighten claims to source evidence."],
            }

    async def revise_briefing(
        self,
        draft_markdown: str,
        items: list[dict],
        feedback: list[str],
        *,
        mode: str = "standard",
        min_chars: int = 1200,
        target_chars: int = 1700,
        hard_max_chars: int = 2300,
    ) -> str:
        """Regenerate a briefing draft using explicit critic/gate feedback."""
        entries: list[str] = []
        for item in items:
            entries.append(await self._build_briefing_entry(item))
        story_cards = await self._build_story_cards(entries, items)
        cards_json = json.dumps({"cards": story_cards}, ensure_ascii=False, indent=2)

        fb = "\n".join(f"- {line}" for line in feedback[:20]) or "- improve overall quality"
        mode_text = (
            "quiet_day: deeper practical guidance with fewer items accepted"
            if mode == "quiet_day"
            else "standard daily briefing"
        )
        user_msg = (
            f"Mode: {mode_text}\n"
            f"Target length: {min_chars}-{hard_max_chars} chars (ideal ~{target_chars}).\n\n"
            "Feedback to address:\n"
            f"{fb}\n\n"
            "Current draft:\n"
            f"{draft_markdown}\n\n"
            "Story cards:\n\n"
            + cards_json
        )
        revised = await self.chat(BRIEFING_REWRITE_PROMPT, user_msg)
        return revised.strip()

    async def generate_cover_prompt(self, topics: str) -> str:
        """Generate a Flux image prompt from today's security topics."""
        user_msg = f"Topics:\n{topics}"
        raw = await self.chat(COVER_ART_SYSTEM_PROMPT, user_msg)
        return raw.replace("`", "").strip()

    def _build_style_memory(self, recent_briefings: list[dict]) -> str:
        """Build compact style-memory snippets to reduce repetitive writing."""
        snippets: list[str] = []
        for idx, briefing in enumerate(recent_briefings[:8], start=1):
            body = self._strip_cover_image(briefing.get("content_markdown", ""))
            lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
            if not lines:
                continue

            headings = re.findall(r"^\*\*(.+?)\*\*$", body, flags=re.MULTILINE)
            opening = lines[0][:140]
            closing = lines[-1][:140]
            snippets.append(
                f"{idx}) opening='{opening}' | headings={headings[:4]} | closing='{closing}'"
            )

        return "\n".join(snippets)

    async def _build_story_cards(self, entries: list[str], items: list[dict]) -> list[dict[str, Any]]:
        """Generate structured story cards before prose generation."""
        user_msg = (
            f"Candidate items ({len(entries)} total):\n\n"
            + "\n\n".join(entries)
        )
        raw = await self.chat(BRIEFING_STORY_CARD_PROMPT, user_msg)
        try:
            cleaned = re.sub(r"```json\s*", "", raw)
            cleaned = re.sub(r"```\s*", "", cleaned)
            parsed = json.loads(cleaned)
            cards = parsed.get("cards", [])
            if not isinstance(cards, list) or not cards:
                raise ValueError("cards missing")
            by_url = {str(item.get("url") or "").strip(): item for item in items}
            normalized: list[dict[str, Any]] = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                main_url = str(card.get("main_url") or "").strip()
                item = by_url.get(main_url)
                if not item:
                    continue
                refs = card.get("reference_links", [])
                if not isinstance(refs, list):
                    refs = []
                cleaned_refs: list[str] = []
                for ref in refs:
                    ref_s = str(ref).strip()
                    if not ref_s:
                        continue
                    if ref_s.startswith(("http://", "https://")):
                        cleaned_refs.append(ref_s)
                normalized.append(
                    {
                        "main_url": main_url,
                        "what_happened": str(card.get("what_happened") or item.get("summary") or ""),
                        "why_now": str(card.get("why_now") or ""),
                        "who_impacted": str(card.get("who_impacted") or ""),
                        "offensive_angle": str(card.get("offensive_angle") or ""),
                        "defensive_action_24h": str(card.get("defensive_action_24h") or ""),
                        "reference_links": cleaned_refs[:3],
                    }
                )
            if normalized:
                return normalized
        except Exception:
            logger.warning("Failed to parse story cards; using deterministic fallback cards")

        return self._fallback_story_cards(items)

    @staticmethod
    def _fallback_story_cards(items: list[dict]) -> list[dict[str, Any]]:
        """Create deterministic baseline cards if LLM carding fails."""
        out: list[dict[str, Any]] = []
        for item in items:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            summary = str(item.get("summary") or "").strip()
            out.append(
                {
                    "main_url": url,
                    "what_happened": summary or str(item.get("title") or ""),
                    "why_now": "Emerging cloud-security signal with near-term operational impact.",
                    "who_impacted": "Cloud operators and teams running production workloads.",
                    "offensive_angle": "Adversaries can abuse the weakness to gain access or evade controls.",
                    "defensive_action_24h": "Patch exposed systems and enable targeted detection immediately.",
                    "reference_links": [],
                }
            )
        return out

    async def _build_briefing_entry(self, item: dict[str, Any]) -> str:
        """Build a single item block for briefing generation."""
        main_url = str(item.get("url") or "").strip()
        ref_urls = self._extract_reference_urls(item.get("raw_data"), main_url)
        ref_urls = await self._filter_live_reference_urls(ref_urls)

        entry = (
            f"- Title: {item.get('title', '')}\n"
            f"  URL: {main_url}\n"
            f"  Summary: {item.get('summary', '')}\n"
            f"  Score: {item.get('relevance_score', 0)}/10\n"
            f"  Source: {item.get('source_type', '')}"
        )
        if ref_urls:
            entry += "\n  Reference links:\n" + "\n".join(f"    - {u}" for u in ref_urls[:3])
        else:
            entry += "\n  Reference links: none"
        return entry

    def _extract_reference_urls(self, raw_data: Any, main_url: str) -> list[str]:
        """Extract and de-duplicate candidate reference URLs from raw source payload."""
        raw: dict[str, Any] = {}
        if isinstance(raw_data, str):
            try:
                raw = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        elif isinstance(raw_data, dict):
            raw = raw_data

        refs = raw.get("references", [])
        seen: set[str] = set()
        out: list[str] = []

        for ref in refs:
            url = ref.get("url", "") if isinstance(ref, dict) else str(ref)
            url = url.strip()
            if (
                not url
                or url in seen
                or url == main_url
                or any(domain in url for domain in _SKIP_DOMAINS)
            ):
                continue
            seen.add(url)
            out.append(url)

        return out

    async def _filter_live_reference_urls(self, urls: list[str]) -> list[str]:
        """Drop broken links for domains where stale references are common."""
        kept: list[str] = []
        for url in urls:
            if await self._is_reference_url_live(url):
                kept.append(url)
            else:
                logger.info("Dropping dead reference URL: %s", url)
        return kept

    async def _is_reference_url_live(self, url: str) -> bool:
        """Check URL liveness for GitHub links to avoid obvious 404 references."""
        if not url.startswith(("http://", "https://")):
            return False
        if "github.com" not in url:
            return True
        if url in self._url_status_cache:
            return self._url_status_cache[url]

        try:
            resp = await self._client.get(url, follow_redirects=True, timeout=10)
            alive = resp.status_code in {200, 401, 403, 405, 429}
        except Exception:
            alive = False

        self._url_status_cache[url] = alive
        return alive

    @staticmethod
    def _strip_cover_image(markdown: str) -> str:
        """Remove leading markdown image syntax from briefing body text."""
        return re.sub(r"^!\[[^\]]*]\([^)]+\)\s*\n*", "", markdown.strip())
