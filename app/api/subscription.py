"""订阅（应用内购买）接口。

客户端在商店完成购买后，把凭证上报到这里；后端必须向 Google Play
校验通过才落库。之后各接口（语音笔记限额、实时转写）只读本地缓存。
"""
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId
from app.infrastructure.database import get_db
from app.schemas.subscription import (
    SubscriptionStatusResponse,
    VerifySubscriptionRequest,
)
from app.services.store_verification import (
    StoreVerificationError,
    VerifiedSubscription,
    verify_store_subscription,
)
from app.services.subscription_service import (
    SubscriptionTokenInUse,
    get_state,
    refresh_subscription_if_stale,
    upsert_subscription,
)

router = APIRouter(tags=["订阅"])
logger = logging.getLogger(__name__)
DbSession = Annotated[AsyncSession, Depends(get_db)]

_ERROR_MESSAGE_KEYS = {
    "verification_not_configured": "subscription_verify_unavailable",
    "store_unavailable": "subscription_verify_unavailable",
    "receipt_required": "subscription_receipt_required",
    "invalid_receipt": "subscription_receipt_invalid",
    "not_active": "subscription_not_active",
    "product_mismatch": "subscription_product_unknown",
    "platform_unsupported": "subscription_platform_unsupported",
}


def _status_response(state) -> SubscriptionStatusResponse:
    return SubscriptionStatusResponse(
        is_subscribed=state.is_subscribed,
        product_id=state.product_id,
        platform=state.platform,
        expires_at=state.expires_at,
        auto_renewing=state.auto_renewing,
        voice_note_count=state.voice_note_count,
        voice_note_limit=state.voice_note_limit,
        can_create_voice_note=state.can_create_voice_note,
        image_note_count=state.image_note_count,
        image_note_limit=state.image_note_limit,
        can_create_image_note=state.can_create_image_note,
        file_note_count=state.file_note_count,
        file_note_limit=state.file_note_limit,
        can_create_file_note=state.can_create_file_note,
    )


@router.get("/subscription", response_model=SubscriptionStatusResponse)
async def get_subscription_status(
    db: DbSession,
    user_id: CurrentUserId,
):
    """当前订阅状态与免费额度用量。App 启动、购买后、保存语音笔记前都可以调。"""
    # 缓存过期时顺带向商店确认；高频业务接口不会走到这里。
    await refresh_subscription_if_stale(db, user_id)
    return _status_response(await get_state(db, user_id))


@router.post("/subscription/verify", response_model=SubscriptionStatusResponse)
async def verify_subscription(
    data: VerifySubscriptionRequest,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """购买 / 续订后上报凭证，后端落库并返回最新订阅状态。"""
    if data.product_id not in {
        settings.SUBSCRIPTION_WEEKLY_PRODUCT_ID,
        settings.SUBSCRIPTION_MONTHLY_PRODUCT_ID,
    }:
        raise HTTPException(
            status_code=400, detail=t(language, "subscription_product_unknown")
        )
    # 至少要有商店凭证，空请求不能解锁。
    if not (data.purchase_token or data.transaction_id):
        raise HTTPException(
            status_code=400, detail=t(language, "subscription_receipt_required")
        )

    if settings.SUBSCRIPTION_VERIFY_WITH_STORE:
        try:
            verified = await verify_store_subscription(
                platform=data.platform,
                product_id=data.product_id,
                purchase_token=data.purchase_token,
                transaction_id=data.transaction_id,
            )
        except StoreVerificationError as exc:
            logger.warning(
                "订阅校验失败: user=%s platform=%s code=%s detail=%s",
                user_id,
                data.platform,
                exc.code,
                exc.detail,
            )
            message_key = _ERROR_MESSAGE_KEYS.get(
                exc.code, "subscription_verify_unavailable"
            )
            raise HTTPException(
                status_code=exc.http_status,
                detail=t(language, message_key),
            ) from exc
    else:
        # 只有本地开发可以显式放开。生产环境 SUBSCRIPTION_VERIFY_WITH_STORE
        # 为 false 时直接拒绝，绝不退回信任客户端。
        if not (settings.DEBUG and settings.SUBSCRIPTION_DEV_ALLOW_UNVERIFIED):
            logger.error(
                "订阅校验未启用，已拒绝上报: user=%s DEBUG=%s",
                user_id,
                settings.DEBUG,
            )
            raise HTTPException(
                status_code=503,
                detail=t(language, "subscription_verify_unavailable"),
            )
        if not (data.purchase_token or data.transaction_id):
            raise HTTPException(
                status_code=400,
                detail=t(language, "subscription_receipt_required"),
            )
        logger.warning(
            "DEV 模式接受未校验的订阅凭证: user=%s platform=%s",
            user_id,
            data.platform,
        )
        now = datetime.now(UTC)
        verified = VerifiedSubscription(
            platform=data.platform,
            product_id=data.product_id,
            purchase_token=data.purchase_token or data.transaction_id or "",
            transaction_id=data.transaction_id,
            expires_at=now
            + timedelta(days=settings.SUBSCRIPTION_FALLBACK_DAYS),
            auto_renewing=True,
            store_state="DEV_UNVERIFIED",
        )

    try:
        await upsert_subscription(db, user_id=user_id, verified=verified)
    except SubscriptionTokenInUse as exc:
        raise HTTPException(
            status_code=409,
            detail=t(language, "subscription_token_in_use"),
        ) from exc
    return _status_response(await get_state(db, user_id))
