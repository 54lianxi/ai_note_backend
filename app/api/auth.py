"""认证接口"""
import hashlib
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId, create_access_token
from app.infrastructure.database import get_db
from app.infrastructure.minio_client import get_minio_client
from app.infrastructure.vector_db import get_vector_client
from app.models.conversation import Conversation, Message
from app.models.record import Record
from app.models.report import AIReport
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.auth import (
    AppleSignInRequest,
    AuthResponse,
    AuthUserResponse,
    GoogleSignInRequest,
    ReviewLoginRequest,
)

router = APIRouter(tags=["认证"])
logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_USER_NAMESPACE = uuid.UUID("6b5ddfd9-86dd-4c1d-87c6-70a069f7343d")
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_USER_NAMESPACE = uuid.UUID("f767f7f3-f1d0-4c7c-8e95-70d05f6af3d4")
REVIEW_USER_NAMESPACE = uuid.UUID("8f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
REVIEW_USERNAME = "review_account"
REVIEW_EMAIL = "review@gmail.com"
_REVIEW_ATTEMPTS: dict[str, list[float]] = {}
_REVIEW_WINDOW_SECONDS = 10 * 60
_REVIEW_MAX_ATTEMPTS = 5
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _review_rate_limited(client_host: str | None) -> bool:
    """限制审核访问码的暴力尝试；多 worker 部署时再由网关限流。"""
    now = time.monotonic()
    key = client_host or "unknown"
    attempts = [
        value
        for value in _REVIEW_ATTEMPTS.get(key, [])
        if now - value < _REVIEW_WINDOW_SECONDS
    ]
    if len(attempts) >= _REVIEW_MAX_ATTEMPTS:
        _REVIEW_ATTEMPTS[key] = attempts
        return True
    attempts.append(now)
    _REVIEW_ATTEMPTS[key] = attempts
    return False


class AppleIdentityVerifier:
    """Apple identity token 校验器"""

    async def verify(self, identity_token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(identity_token)
        except JWTError as e:
            raise HTTPException(status_code=401, detail="Apple identity token 无效") from e

        kid = header.get("kid")
        alg = header.get("alg", "RS256")
        key = await self._get_public_key(kid)
        if key is None:
            raise HTTPException(status_code=401, detail="未找到匹配的 Apple 公钥")

        try:
            return jwt.decode(
                identity_token,
                key,
                algorithms=[alg],
                audience=settings.APPLE_CLIENT_ID,
                issuer=APPLE_ISSUER,
            )
        except JWTError as e:
            raise HTTPException(status_code=401, detail="Apple identity token 校验失败") from e

    async def _get_public_key(self, kid: str | None) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(settings.APPLE_JWKS_URL)
            response.raise_for_status()

        keys = response.json().get("keys", [])
        for key in keys:
            if key.get("kid") == kid:
                return key
        return None


def _apple_user_id(apple_sub: str) -> uuid.UUID:
    return uuid.uuid5(APPLE_USER_NAMESPACE, apple_sub)


def _apple_username(apple_sub: str) -> str:
    digest = hashlib.sha256(apple_sub.encode("utf-8")).hexdigest()[:24]
    return f"apple_{digest}"


class GoogleIdentityVerifier:
    """Google identity token 校验器"""

    async def verify(self, identity_token: str) -> dict[str, Any]:
        audiences = {
            client_id.strip()
            for client_id in settings.GOOGLE_CLIENT_IDS.split(",")
            if client_id.strip()
        }
        if not audiences:
            raise HTTPException(status_code=503, detail="Google 登录尚未配置")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    settings.GOOGLE_TOKEN_INFO_URL,
                    params={"id_token": identity_token},
                )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail="无法连接 Google 身份服务") from e

        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Google identity token 无效")

        try:
            payload = response.json()
        except ValueError as e:
            raise HTTPException(status_code=401, detail="Google identity token 响应无效") from e

        if payload.get("aud") not in audiences:
            raise HTTPException(status_code=401, detail="Google identity token audience 不匹配")
        if payload.get("iss") not in GOOGLE_ISSUERS:
            raise HTTPException(status_code=401, detail="Google identity token 签发方无效")
        if payload.get("email_verified") != "true":
            raise HTTPException(status_code=401, detail="Google 邮箱尚未验证")
        if not payload.get("sub") or not payload.get("email"):
            raise HTTPException(status_code=401, detail="Google identity token 缺少用户信息")

        return payload


def _google_user_id(google_sub: str) -> uuid.UUID:
    return uuid.uuid5(GOOGLE_USER_NAMESPACE, google_sub)


def _google_username(google_sub: str) -> str:
    digest = hashlib.sha256(google_sub.encode("utf-8")).hexdigest()[:24]
    return f"google_{digest}"


@router.post("/auth/apple", response_model=AuthResponse)
async def sign_in_with_apple(
    data: AppleSignInRequest,
    db: DbSession,
):
    """使用 Apple identity token 登录或创建本地用户"""
    payload = await AppleIdentityVerifier().verify(data.identity_token)
    apple_sub = payload.get("sub")
    if not apple_sub:
        raise HTTPException(status_code=401, detail="Apple identity token 缺少用户标识")

    email = data.email or payload.get("email")
    user_id = _apple_user_id(str(apple_sub))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=_apple_username(str(apple_sub)),
            hashed_password="apple-sign-in",
            email=email,
        )
        db.add(user)
        await db.flush()
    elif email and user.email != email:
        # Apple 只在首次授权返回邮箱，之后要沿用库里存的值。
        user.email = email

    access_token = create_access_token({"sub": str(user.id)})
    return AuthResponse(
        access_token=access_token,
        user=AuthUserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
        ),
    )


@router.post("/auth/google", response_model=AuthResponse)
async def sign_in_with_google(
    data: GoogleSignInRequest,
    db: DbSession,
):
    """使用 Google identity token 登录或创建本地用户"""
    payload = await GoogleIdentityVerifier().verify(data.identity_token)
    google_sub = str(payload["sub"])
    email = payload.get("email")
    user_id = _google_user_id(google_sub)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=_google_username(google_sub),
            hashed_password="google-sign-in",
            email=email,
        )
        db.add(user)
        await db.flush()
    elif email and user.email != email:
        user.email = email

    access_token = create_access_token({"sub": str(user.id)})
    return AuthResponse(
        access_token=access_token,
        user=AuthUserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
        ),
    )


@router.post("/auth/review", response_model=AuthResponse)
async def sign_in_as_review_account(
    data: ReviewLoginRequest,
    request: Request,
    db: DbSession,
):
    """应用商店审核账号：访问码只存在服务端，并授予演示用订阅。"""
    if not settings.REVIEW_LOGIN_ENABLED:
        raise HTTPException(status_code=404, detail="审核账号入口已关闭")
    if not settings.REVIEW_ACCESS_CODE:
        raise HTTPException(status_code=503, detail="审核账号访问码尚未配置")
    if len(settings.REVIEW_ACCESS_CODE) < 12:
        raise HTTPException(status_code=503, detail="审核账号访问码过短")
    if _review_rate_limited(request.client.host if request.client else None):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    if not secrets.compare_digest(data.access_code, settings.REVIEW_ACCESS_CODE):
        raise HTTPException(status_code=401, detail="审核访问码不正确")

    user_id = uuid.uuid5(REVIEW_USER_NAMESPACE, REVIEW_USERNAME)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=REVIEW_USERNAME,
            hashed_password="review-sign-in",
            email=REVIEW_EMAIL,
        )
        db.add(user)
        await db.flush()

    # 审核员必须能测实时转写、语音输入和去广告；只给演示账号这段订阅。
    subscription = (
        await db.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if subscription is None:
        subscription = Subscription(
            user_id=user_id,
            product_id="review-entitlement",
            platform="review",
        )
        db.add(subscription)
    subscription.status = "active"
    subscription.auto_renewing = False
    subscription.expires_at = now + timedelta(days=3650)
    subscription.last_verified_at = now

    access_token = create_access_token({"sub": str(user.id)})
    return AuthResponse(
        access_token=access_token,
        user=AuthUserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
        ),
    )


def _object_name_from_url(bucket: str, url: str) -> str | None:
    """从对象存储 URL 反推 MinIO 的 object key，取不到时返回 None。"""
    marker = f"/{bucket}/"
    index = url.find(marker)
    if index == -1:
        return None
    return url[index + len(marker):]


@router.delete("/auth/account")
async def delete_account(
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """注销账户：删除用户及其全部数据（记录、对话、消息、向量、存储文件）。"""
    uid = uuid.UUID(user_id)
    result = await db.execute(select(User).where(User.id == uid))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=t(language, "account_not_found"))

    records = (
        await db.execute(select(Record).where(Record.user_id == uid))
    ).scalars().all()

    # 先清外部存储：删库之后就再也定位不到这些文件了。失败只记日志，不阻塞注销。
    minio = None
    for record in records:
        if record.vector_id:
            try:
                get_vector_client().delete_record(str(record.id), user_id)
            except Exception:
                logger.warning("注销账户删除向量失败: %s", record.vector_id, exc_info=True)
        for url in (record.image_url, record.file_url):
            if not url:
                continue
            try:
                if minio is None:
                    minio = get_minio_client()
                object_name = _object_name_from_url(minio.bucket, url)
                if object_name:
                    minio.delete_file(object_name)
            except Exception:
                logger.warning("注销账户删除文件失败: %s", url, exc_info=True)

    # 按外键依赖顺序清理，最后删用户本身。
    conversation_ids = select(Conversation.id).where(Conversation.user_id == uid)
    await db.execute(
        delete(Message).where(Message.conversation_id.in_(conversation_ids))
    )
    await db.execute(delete(Conversation).where(Conversation.user_id == uid))
    record_ids = select(Record.id).where(Record.user_id == uid)
    await db.execute(delete(Message).where(Message.record_id.in_(record_ids)))
    await db.execute(delete(Record).where(Record.user_id == uid))
    await db.execute(delete(AIReport).where(AIReport.user_id == uid))
    await db.execute(delete(Subscription).where(Subscription.user_id == uid))
    await db.execute(delete(User).where(User.id == uid))

    return {"message": t(language, "account_deleted")}
