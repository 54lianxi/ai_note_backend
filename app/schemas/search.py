"""检索相关 Pydantic Schema"""
import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    """检索请求"""
    conversation_id: uuid.UUID
    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)


class SearchResultItem(BaseModel):
    """检索结果项"""
    record_id: uuid.UUID
    content: str
    summary: str | None
    score: float
    content_type: str
    image_url: str | None = None
    created_at: datetime


class SearchResponse(BaseModel):
    """检索响应"""
    answer: str  # LLM 生成的自然语言回答
    results: list[SearchResultItem]
