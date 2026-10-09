"""对话相关 Pydantic Schema"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.record import RecordResponse
from app.schemas.search import SearchResultItem


class ConversationCreate(BaseModel):
    """创建对话请求"""
    title: str = Field(default="新对话", max_length=200)


class TextMessageRequest(BaseModel):
    """统一文本输入请求"""
    conversation_id: uuid.UUID
    content: str = Field(..., min_length=1, max_length=10000)


class TextMessageResponse(BaseModel):
    """统一文本输入响应"""
    intent: Literal["record", "search"]
    message: str
    record: RecordResponse | None = None
    answer: str | None = None
    results: list[SearchResultItem] = Field(default_factory=list)


class ConversationResponse(BaseModel):
    """对话响应"""
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime
    
    model_config = {"from_attributes": True}


class ConversationListResponse(BaseModel):
    """对话列表响应"""
    conversations: list[ConversationResponse]
    total: int


class MessageResponse(BaseModel):
    """消息响应"""
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    content_type: str
    record_id: uuid.UUID | None
    related_record_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime

    @field_validator("related_record_ids", mode="before")
    @classmethod
    def _none_as_empty(cls, value: object) -> object:
        # 历史消息该字段为 NULL，统一按空列表处理。
        return value or []

    model_config = {"from_attributes": True}


class ConversationDetailResponse(BaseModel):
    """对话详情（含消息）"""
    id: uuid.UUID
    title: str
    messages: list[MessageResponse]
    created_at: datetime
    
    model_config = {"from_attributes": True}
