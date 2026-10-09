"""记录模型"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class Record(Base):
    """记录表"""
    __tablename__ = "records"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    content: Mapped[str] = mapped_column(Text)  # 原始文本内容
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 摘要
    content_type: Mapped[str] = mapped_column(String(20))  # text/voice/image
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # MinIO 图片地址
    image_key_info: Mapped[str | None] = mapped_column(Text, nullable=True)  # 视觉模型提取的关键信息
    vector_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # 向量数据库中的 ID
    file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 通用附件地址
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int | None] = mapped_column(nullable=True)
    # 统一以 UTC 存储，列带时区（TIMESTAMPTZ），展示交给前端按本地时区处理。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )
    
    # 关系
    user = relationship("User", back_populates="records")
    messages = relationship("Message", back_populates="record")
