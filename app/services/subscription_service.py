"""订阅状态与免费额度。

免费用户：
- 不能使用语音笔记的实时转写、AI 对话的语音输入（由客户端与实时转写接口拦截）；
- 最多保存 5 条语音笔记；
- 会看到广告（客户端展示）。
订阅用户：以上全部解锁。
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.record import Record
from app.models.subscription import Subscription

logger = logging.getLogger(__name__)

# 未订阅用户最多能保存多少条语音笔记。
FREE_VOICE_NOTE_LIMIT = settings.SUBSCRIPTION_FREE_VOICE_LIMIT


@dataclass(frozen=True)
class SubscriptionState:
    """给客户端的订阅与额度快照。"""

    is_subscribed: bool
    product_id: str | None
    platform: str | None
    expires_at: datetime | None
    auto_renewing: bool
    voice_note_count: int
    voice_note_limit: int | None  # None 表示不限量

    @property
    def can_create_voice_note(self) -> bool:
        if self.is_subscribed:
            return True
        return self.voice_note_count < FREE_VOICE_NOTE_LIMIT


def _is_active(subscription: Subscription | None, now: datetime) -> bool:
    if subscription is None or subscription.status != "active":
        return False
    expires_at = subscription.expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now


async def get_subscription(
    db: AsyncSession, user_id: str | uuid.UUID
) -> Subscription | None:
    """取当前用户的订阅记录（没有则返回 None）。"""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == uid)
    )
    return result.scalar_one_or_none()


async def count_voice_notes(
    db: AsyncSession, user_id: str | uuid.UUID
) -> int:
    """已经保存的语音笔记条数（待同步的离线笔记不计入）。"""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    return (
        await db.execute(
            select(func.count())
            .select_from(Record)
            .where(Record.user_id == uid, Record.content_type == "voice")
        )
    ).scalar_one()


async def get_state(db: AsyncSession, user_id: str | uuid.UUID) -> SubscriptionState:
    """汇总订阅状态 + 免费额度用量，供接口和限额判断共用。"""
    subscription = await get_subscription(db, user_id)
    active = _is_active(subscription, datetime.now(UTC))
    count = await count_voice_notes(db, user_id)
    return SubscriptionState(
        is_subscribed=active,
        product_id=subscription.product_id if subscription else None,
        platform=subscription.platform if subscription else None,
        expires_at=subscription.expires_at if subscription else None,
        auto_renewing=bool(subscription.auto_renewing) if subscription else False,
        voice_note_count=count,
        voice_note_limit=None if active else FREE_VOICE_NOTE_LIMIT,
    )


async def is_subscribed(db: AsyncSession, user_id: str | uuid.UUID) -> bool:
    """供各接口做权限判断。"""
    subscription = await get_subscription(db, user_id)
    return _is_active(subscription, datetime.now(UTC))


async def upsert_subscription(
    db: AsyncSession,
    *,
    user_id: str | uuid.UUID,
    platform: str,
    product_id: str,
    purchase_token: str | None = None,
    transaction_id: str | None = None,
    expires_at: datetime | None = None,
    auto_renewing: bool = True,
) -> Subscription:
    """写入 / 更新订阅记录。

    没有接商店服务端校验（Google Play Developer API、Apple App Store Server API）
    时，按客户端上报的结果授权；即使客户端没带 expires_at，也兜底给一个周期，
    免得订阅到期后无限期可用。接入校验后改用商店返回的到期时间即可。
    """
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(
            days=settings.SUBSCRIPTION_FALLBACK_DAYS
        )
    elif expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    subscription = await get_subscription(db, uid)
    if subscription is None:
        subscription = Subscription(user_id=uid, platform=platform, product_id=product_id)
        db.add(subscription)

    subscription.platform = platform
    subscription.product_id = product_id
    subscription.purchase_token = purchase_token
    subscription.original_transaction_id = transaction_id
    subscription.auto_renewing = auto_renewing
    subscription.expires_at = expires_at
    subscription.status = "active" if expires_at > datetime.now(UTC) else "expired"

    await db.flush()
    if not settings.SUBSCRIPTION_VERIFY_WITH_STORE:
        logger.info(
            "已按客户端上报授权订阅（未接入商店服务端校验）: user=%s product=%s",
            uid,
            product_id,
        )
    return subscription
