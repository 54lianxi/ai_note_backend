"""记录相关 Pydantic Schema"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TextRecordCreate(BaseModel):
    """文本记录上传请求"""
    # 笔记已与对话解耦，conversation_id 仅为兼容旧客户端保留。
    conversation_id: uuid.UUID | None = None
    content: str = Field(..., min_length=1, max_length=10000)


class RecordResponse(BaseModel):
    """记录响应"""
    record_id: uuid.UUID
    message: str = "记录成功"
    content: str | None = None
    summary: str | None = None
    content_type: str
    image_url: str | None = None
    image_key_info: str | None = None
    file_url: str | None = None
    file_name: str | None = None
    file_content_type: str | None = None
    file_size: int | None = None
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class RecordUpdate(BaseModel):
    """编辑记录请求"""
    content: str = Field(..., min_length=1, max_length=10000)
    summary: str | None = Field(default=None, max_length=500)


class RecordListResponse(BaseModel):
    """记录列表响应"""
    records: list[RecordResponse]
    total: int


class VoiceTranscriptionResponse(BaseModel):
    """语音转写响应，不直接写入记录"""
    text: str
    summary: str


class ImageKeyInfo(BaseModel):
    """图片关键信息"""
    extracted_text: str | None = None
    description: str | None = None
    summary: str | None = None
