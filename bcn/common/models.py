"""Pydantic data models for news items, analysis results, and briefings."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field


class NewsItem(BaseModel):
    """A single collected news item from any source."""

    id: UUID
    source_type: str
    source_id: str
    url: str
    title: Optional[str] = None
    published_at: datetime
    raw_data: Optional[dict] = None
    full_content: Optional[str] = None
    summary: Optional[str] = None
    relevance_score: Optional[int] = None
    ai_tags: Optional[list[str]] = None
    image_prompt: Optional[str] = None
    image_url: Optional[str] = None
    status: str = "NEW"


class AnalysisResult(BaseModel):
    """LLM analysis output for a single news item."""

    summary: str
    relevance_score: int = Field(ge=1, le=10)
    tags: list[str] = Field(default_factory=list)
    image_prompt: str = "cloud security concept art"
    canonical_url: Optional[str] = Field(
        default=None,
        description="The most authoritative primary source URL found, if any.",
    )


class AnalyzedItemUpdate(BaseModel):
    """Persistable analyst result returned by the analyst domain service."""

    summary: str
    relevance_score: int = Field(ge=1, le=10)
    ai_tags: list[str] = Field(default_factory=list)
    full_content: Optional[str] = None
    image_prompt: Optional[str] = None
    canonical_url: Optional[str] = None


class CollectedNewsItem(BaseModel):
    """Persistable collected item returned by the collector domain service."""

    source_type: str
    source_id: str
    url: str
    title: Optional[str] = None
    published_at: datetime | str
    raw_data: dict = Field(default_factory=dict)
    full_content: Optional[str] = None


class CollectionSourceReview(BaseModel):
    """LLM review result for onboarding a new collection source."""

    decision: Literal["promote", "quarantine"] = "quarantine"
    confidence: Literal["low", "medium", "high"] = "medium"
    rationale: str = ""
    signals: list[str] = Field(default_factory=list)


class Briefing(BaseModel):
    """A generated daily briefing with optional cover image."""

    id: UUID
    created_at: datetime
    content_markdown: str
    content_html: Optional[str] = None
    cover_image_url: Optional[str] = None
    cover_image_prompt: Optional[str] = None
    item_ids: list[UUID] = Field(
        default_factory=list,
        description="Ordered list of associated news item ids.",
    )
    status: str = "DRAFT"
    distributed_at: Optional[datetime] = None
    distribution_channels: Optional[dict] = None


class GenerationRoundArtifact(BaseModel):
    """Artifacts captured for one writer evaluate/rewrite round."""

    round_index: int
    phase: str = "initial"
    draft_input: str
    gate_result: dict = Field(default_factory=dict)
    critique_result: dict = Field(default_factory=dict)
    verifier_result: dict = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)
    rewrite_output: Optional[str] = None
    passed: bool = False


class GenerationRunTrace(BaseModel):
    """Full generation trace for one writer attempt."""

    id: UUID
    created_at: datetime
    mode: str = "standard"
    decision: str = "PENDING"
    rewrite_count: int = 0
    briefing_id: Optional[UUID] = None
    selected_item_ids: list[UUID] = Field(default_factory=list)
    selected_items: list[dict] = Field(default_factory=list)
    llm_model: Optional[str] = None
    llm_model_version: Optional[str] = None
    prompts: dict = Field(default_factory=dict)
    config_snapshot: dict = Field(default_factory=dict)
    git_sha: Optional[str] = None
    initial_draft: Optional[str] = None
    final_draft: Optional[str] = None
    final_gate: dict = Field(default_factory=dict)
    final_critique: dict = Field(default_factory=dict)
    final_verifier: dict = Field(default_factory=dict)


class HumanReview(BaseModel):
    """Human review feedback tied to a briefing/run."""

    id: UUID
    briefing_id: UUID
    run_id: Optional[UUID] = None
    created_at: datetime
    reviewer: str = "cli"
    decision: str
    issue_tags: list[str] = Field(default_factory=list)
    edited_markdown: Optional[str] = None
    notes: Optional[str] = None


class DistributionOutcome(BaseModel):
    """Channel-level distribution status and engagement signals."""

    id: int
    briefing_id: UUID
    channel: str
    status: str
    sent_at: datetime
    external_message_id: Optional[str] = None
    external_post_url: Optional[str] = None
    metrics: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
