"""订阅（应用内购买）模型"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base


class Subscription(Base):
    """用户订阅状态。

    一个用户只保留一条最新记录（user_id 唯一），购买 / 续订 / 恢复时覆盖更新。
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    # 商店商品 ID（App Store / Google Play 用同一个）。
    product_id: Mapped[str] = mapped_column(String(100))
    # android / ios。
    platform: Mapped[str] = mapped_column(String(20))
    # 商店的购买凭证，用于后续服务端校验 / 恢复。
    purchase_token: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    original_transaction_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # active / expired。
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    auto_renewing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 到期时间（UTC）；为空表示永久有效（例如本地开发解锁）。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=True,
    )

    user = relationship("User", back_populates="subscription")
