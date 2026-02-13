from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class NewsItem(BaseModel):
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
    summary: str
    relevance_score: int = Field(ge=1, le=10)
    tags: list[str] = Field(default_factory=list)
    image_prompt: str = "cloud security concept art"


class Briefing(BaseModel):
    id: UUID
    created_at: datetime
    content_markdown: str
    content_html: Optional[str] = None
    cover_image_url: Optional[str] = None
    cover_image_prompt: Optional[str] = None
    item_ids: list[UUID] = Field(default_factory=list)
    status: str = "DRAFT"
    distributed_at: Optional[datetime] = None
    distribution_channels: Optional[dict] = None
