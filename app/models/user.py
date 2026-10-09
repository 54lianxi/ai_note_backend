"""用户模型"""
import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class User(Base):
    """用户表"""
    __tablename__ = "users"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    # 统一以 UTC 存储（TIMESTAMPTZ），前端按用户本地时区展示。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    
    # 关系
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    records = relationship("Record", back_populates="user", cascade="all, delete-orphan")
