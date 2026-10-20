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
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.record import Record
from app.models.subscription import Subscription
from app.services.store_verification import (
    StoreVerificationError,
    VerifiedSubscription,
    verify_store_subscription,
)

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


class SubscriptionTokenInUse(Exception):
    """同一个商店凭证已经绑定到别的账号。"""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_active(subscription: Subscription | None, now: datetime) -> bool:
    """判断订阅当前是否有效。

    开启商店校验后，只有经过服务端验证的记录才算数。修复前由客户端直接
    上报的老数据 `last_verified_at` 为空，必须先在 /subscription 刷新时
    重新验证，验证通过前不授予任何权益。
    """
    if subscription is None or subscription.status != "active":
        return False
    if (
        settings.SUBSCRIPTION_VERIFY_WITH_STORE
        and subscription.last_verified_at is None
    ):
        return False
    expires_at = _as_utc(subscription.expires_at)
    if expires_at is None:
        return True
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
        # 未验证 / 已失效的记录只用于内部排查，不把过期信息当成有效权益返回。
        product_id=subscription.product_id if active else None,
        platform=subscription.platform if active else None,
        expires_at=subscription.expires_at if active else None,
        auto_renewing=bool(subscription.auto_renewing) if active else False,
        voice_note_count=count,
        voice_note_limit=None if active else FREE_VOICE_NOTE_LIMIT,
    )


async def is_subscribed(db: AsyncSession, user_id: str | uuid.UUID) -> bool:
    """供各接口做权限判断。

    只看本地已验证的缓存，避免每次保存笔记 / 建实时转写连接都打 Google API。
    """
    subscription = await get_subscription(db, user_id)
    return _is_active(subscription, datetime.now(UTC))


def _apply_verified(
    subscription: Subscription, verified: VerifiedSubscription
) -> None:
    now = datetime.now(UTC)
    subscription.platform = verified.platform
    subscription.product_id = verified.product_id
    subscription.purchase_token = verified.purchase_token
    subscription.original_transaction_id = verified.transaction_id
    subscription.auto_renewing = verified.auto_renewing
    subscription.expires_at = verified.expires_at
    subscription.status = "active" if verified.expires_at > now else "expired"
    subscription.last_verified_at = now


async def upsert_subscription(
    db: AsyncSession,
    *,
    user_id: str | uuid.UUID,
    verified: VerifiedSubscription,
) -> Subscription:
    """写入 / 更新已经过商店服务端验证的订阅记录。"""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)

    # 一份购买只能绑定一个账号，防止把同一个 purchaseToken 分享给多个人。
    if verified.purchase_token:
        other = (
            await db.execute(
                select(Subscription.id).where(
                    Subscription.purchase_token == verified.purchase_token,
                    Subscription.user_id != uid,
                )
            )
        ).scalar_one_or_none()
        if other is not None:
            raise SubscriptionTokenInUse

    subscription = await get_subscription(db, uid)
    if subscription is None:
        subscription = Subscription(
            user_id=uid,
            platform=verified.platform,
            product_id=verified.product_id,
        )
        db.add(subscription)

    _apply_verified(subscription, verified)

    await db.flush()
    return subscription


async def refresh_subscription_if_stale(
    db: AsyncSession, user_id: str | uuid.UUID
) -> None:
    """缓存过期时向商店重新确认一次。

    只在拉取订阅状态（App 启动、进订阅页）时调用；保存笔记、实时转写等
    高频路径只读本地结果。
    """
    if not settings.SUBSCRIPTION_VERIFY_WITH_STORE:
        return

    subscription = await get_subscription(db, user_id)
    if subscription is None or not subscription.purchase_token:
        return

    now = datetime.now(UTC)
    last_verified = _as_utc(subscription.last_verified_at)
    ttl = max(60, settings.SUBSCRIPTION_CACHE_TTL_SECONDS)
    if (
        last_verified is not None
        and (now - last_verified).total_seconds() < ttl
    ):
        return

    try:
        verified = await verify_store_subscription(
            platform=subscription.platform,
            product_id=subscription.product_id,
            purchase_token=subscription.purchase_token,
            transaction_id=subscription.original_transaction_id,
        )
    except StoreVerificationError as exc:
        if exc.code in {
            "invalid_receipt",
            "not_active",
            "product_mismatch",
            "platform_unsupported",
        }:
            # 商店明确说这笔订阅无效了，立即收回权益。
            subscription.status = "expired"
            subscription.auto_renewing = False
            subscription.last_verified_at = now
            logger.info(
                "订阅已被商店判定失效: user=%s code=%s",
                user_id,
                exc.code,
            )
        else:
            # Google 临时不可用时不把已确认过的用户踢下线；
            # 从未验证过的记录因为 last_verified_at 为空，仍然不会授权。
            logger.warning(
                "订阅刷新失败，暂时保留上次状态: user=%s code=%s detail=%s",
                user_id,
                exc.code,
                exc.detail,
            )
        return
    except Exception:
        logger.exception("订阅刷新出现未预期错误: user=%s", user_id)
        return

    _apply_verified(subscription, verified)
