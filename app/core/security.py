"""安全工具 - JWT 认证"""
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)
DEV_USER_ID = "00000000-0000-0000-0000-000000000001"
DEV_ACCESS_TOKEN = "dev-local-token"


def hash_password(password: str) -> str:
    """密码哈希"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """创建 JWT Token"""
    to_encode = data.copy()
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """解析 JWT Token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """FastAPI 依赖：从 Bearer Token 读取当前用户 ID"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="请先登录")

    if (
        settings.DEBUG
        and settings.DEV_AUTH_BYPASS
        and credentials.credentials == DEV_ACCESS_TOKEN
    ):
        return DEV_USER_ID

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return str(user_id)


CurrentUserId = Annotated[str, Depends(get_current_user_id)]


def get_websocket_user_id(token: str | None) -> str:
    """WebSocket 认证：沿用 HTTP Bearer JWT 和本地开发令牌。"""
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")

    if (
        settings.DEBUG
        and settings.DEV_AUTH_BYPASS
        and token == DEV_ACCESS_TOKEN
    ):
        return DEV_USER_ID

    payload = decode_access_token(token)
    user_id = payload.get("sub") if payload else None
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return str(user_id)
