"""AI 回复举报请求。"""

import uuid

from pydantic import BaseModel, Field


class AIReportCreate(BaseModel):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    reason: str = Field(..., min_length=1, max_length=50)
    details: str | None = Field(default=None, max_length=1000)
