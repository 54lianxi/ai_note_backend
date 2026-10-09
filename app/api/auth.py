"""认证接口"""
import hashlib
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_access_token
from app.infrastructure.database import get_db
from app.models.user import User
from app.schemas.auth import (
    AppleSignInRequest,
    AuthResponse,
    AuthUserResponse,
    GoogleSignInRequest,
)

router = APIRouter(tags=["认证"])

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_USER_NAMESPACE = uuid.UUID("6b5ddfd9-86dd-4c1d-87c6-70a069f7343d")
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_USER_NAMESPACE = uuid.UUID("f767f7f3-f1d0-4c7c-8e95-70d05f6af3d4")
DbSession = Annotated[AsyncSession, Depends(get_db)]


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

    user_id = _apple_user_id(str(apple_sub))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=_apple_username(str(apple_sub)),
            hashed_password="apple-sign-in",
        )
        db.add(user)
        await db.flush()

    access_token = create_access_token({"sub": str(user.id)})
    return AuthResponse(
        access_token=access_token,
        user=AuthUserResponse(
            id=user.id,
            username=user.username,
            email=data.email or payload.get("email"),
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
    user_id = _google_user_id(google_sub)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=user_id,
            username=_google_username(google_sub),
            hashed_password="google-sign-in",
        )
        db.add(user)
        await db.flush()

    access_token = create_access_token({"sub": str(user.id)})
    return AuthResponse(
        access_token=access_token,
        user=AuthUserResponse(
            id=user.id,
            username=user.username,
            email=payload.get("email"),
        ),
    )
