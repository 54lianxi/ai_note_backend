"""认证相关 Schema"""
import uuid

from pydantic import BaseModel, Field


class AppleSignInRequest(BaseModel):
    """Apple 登录请求"""
    identity_token: str = Field(..., min_length=20)
    email: str | None = None
    full_name: str | None = None


class GoogleSignInRequest(BaseModel):
    """Google 登录请求"""
    identity_token: str = Field(..., min_length=20)


class AuthUserResponse(BaseModel):
    """登录用户信息"""
    id: uuid.UUID
    username: str
    email: str | None = None


class AuthResponse(BaseModel):
    """登录响应"""
    access_token: str
    token_type: str = "bearer"
    user: AuthUserResponse
