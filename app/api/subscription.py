"""订阅（应用内购买）接口。

客户端在 App Store / Google Play 完成购买后，把凭证上报到这里落库；
之后各接口（语音笔记限额、实时转写）都用这里的订阅状态做判断。
"""
import logging
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
from app.services.subscription_service import (
    get_state,
    upsert_subscription,
)

router = APIRouter(tags=["订阅"])
logger = logging.getLogger(__name__)
DbSession = Annotated[AsyncSession, Depends(get_db)]


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
    )


@router.get("/subscription", response_model=SubscriptionStatusResponse)
async def get_subscription_status(
    db: DbSession,
    user_id: CurrentUserId,
):
    """当前订阅状态与免费额度用量。App 启动、购买后、保存语音笔记前都可以调。"""
    return _status_response(await get_state(db, user_id))


@router.post("/subscription/verify", response_model=SubscriptionStatusResponse)
async def verify_subscription(
    data: VerifySubscriptionRequest,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """购买 / 续订后上报凭证，后端落库并返回最新订阅状态。"""
    if data.product_id != settings.SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(
            status_code=400, detail=t(language, "subscription_product_unknown")
        )
    # 至少要有商店凭证，空请求不能解锁。
    if not (data.purchase_token or data.transaction_id):
        raise HTTPException(
            status_code=400, detail=t(language, "subscription_receipt_required")
        )

    await upsert_subscription(
        db,
        user_id=user_id,
        platform=data.platform,
        product_id=data.product_id,
        purchase_token=data.purchase_token,
        transaction_id=data.transaction_id,
        expires_at=data.expires_at,
        auto_renewing=data.auto_renewing,
    )
    return _status_response(await get_state(db, user_id))
