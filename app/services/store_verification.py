"""应用商店服务端校验。

客户端上报的凭证只是“线索”，能不能解锁由这里向商店查询后决定。
目前接入了 Google Play（subscriptionsv2）；iOS 还没接 App Store Server API，
所以直接拒绝，避免又退回“信任客户端”的授权方式。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from jose import jwt

from app.config import settings
from app.core.http import build_async_client

logger = logging.getLogger(__name__)

ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# 这些状态下用户仍然可以使用订阅权益：
# - ACTIVE：正常订阅；
# - CANCELED：已取消续订，但当前周期还没结束；
# - IN_GRACE_PERIOD：扣款失败，Google 给了一段宽限期。
_ENTITLED_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_CANCELED",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
}


class StoreVerificationError(Exception):
    """商店校验失败。code 供接口层映射成用户能看懂的文案。"""

    def __init__(
        self,
        code: str,
        *,
        http_status: int = 400,
        detail: str = "",
    ) -> None:
        super().__init__(detail or code)
        self.code = code
        self.http_status = http_status
        self.detail = detail


@dataclass(frozen=True)
class VerifiedSubscription:
    """经过商店服务端确认后的订阅快照。"""

    platform: str
    product_id: str
    purchase_token: str
    transaction_id: str | None
    expires_at: datetime
    auto_renewing: bool
    store_state: str


_access_token: str | None = None
_access_token_expires_at = 0.0
_access_token_lock = asyncio.Lock()


def _parse_time(value: str) -> datetime:
    """解析 Google 返回的 RFC3339 时间。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _service_account_info() -> dict[str, Any]:
    raw = settings.GOOGLE_PLAY_SERVICE_ACCOUNT_JSON.strip()
    if not raw:
        path_value = settings.GOOGLE_PLAY_SERVICE_ACCOUNT_FILE.strip()
        if path_value:
            path = Path(path_value).expanduser()
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise StoreVerificationError(
                    "verification_not_configured",
                    http_status=503,
                    detail=f"无法读取 Google Play 服务账号文件: {path}",
                ) from exc

    if not raw:
        raise StoreVerificationError(
            "verification_not_configured",
            http_status=503,
            detail=(
                "未配置 GOOGLE_PLAY_SERVICE_ACCOUNT_FILE 或 "
                "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"
            ),
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreVerificationError(
            "verification_not_configured",
            http_status=503,
            detail="Google Play 服务账号 JSON 格式错误",
        ) from exc

    if not data.get("client_email") or not data.get("private_key"):
        raise StoreVerificationError(
            "verification_not_configured",
            http_status=503,
            detail="Google Play 服务账号缺少 client_email 或 private_key",
        )
    return data


async def _get_access_token() -> str:
    """用服务账号换 Google OAuth 访问令牌，并在内存里缓存到过期前。"""
    global _access_token, _access_token_expires_at

    if _access_token and time.monotonic() < _access_token_expires_at - 60:
        return _access_token

    async with _access_token_lock:
        if _access_token and time.monotonic() < _access_token_expires_at - 60:
            return _access_token

        info = _service_account_info()
        now = int(time.time())
        claims = {
            "iss": info["client_email"],
            "scope": ANDROID_PUBLISHER_SCOPE,
            "aud": info.get("token_uri") or settings.GOOGLE_OAUTH_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        try:
            assertion = jwt.encode(
                claims, info["private_key"], algorithm="RS256"
            )
        except Exception as exc:
            raise StoreVerificationError(
                "verification_not_configured",
                http_status=503,
                detail="Google Play 服务账号私钥无法签名",
            ) from exc

        token_url = info.get("token_uri") or settings.GOOGLE_OAUTH_TOKEN_URL
        try:
            async with build_async_client(timeout=15.0) as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": (
                            "urn:ietf:params:oauth:grant-type:jwt-bearer"
                        ),
                        "assertion": assertion,
                    },
                )
        except httpx.HTTPError as exc:
            raise StoreVerificationError(
                "store_unavailable",
                http_status=503,
                detail="连接 Google OAuth 失败",
            ) from exc

        if response.status_code >= 400:
            logger.error(
                "Google OAuth 获取访问令牌失败: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise StoreVerificationError(
                "store_unavailable",
                http_status=503,
                detail="Google OAuth 拒绝服务账号",
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise StoreVerificationError(
                "store_unavailable",
                http_status=503,
                detail="Google OAuth 返回了非 JSON 内容",
            ) from exc
        token = payload.get("access_token")
        if not token:
            raise StoreVerificationError(
                "store_unavailable",
                http_status=503,
                detail="Google OAuth 未返回 access_token",
            )

        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        _access_token = str(token)
        _access_token_expires_at = time.monotonic() + expires_in
        return _access_token


def _subscription_url(purchase_token: str, *, acknowledge: bool = False) -> str:
    base = settings.GOOGLE_PLAY_API_BASE.rstrip("/")
    package = quote(settings.GOOGLE_PLAY_PACKAGE_NAME, safe="")
    token = quote(purchase_token, safe="")
    suffix = ":acknowledge" if acknowledge else ""
    return (
        f"{base}/applications/{package}/purchases/"
        f"subscriptionsv2/tokens/{token}{suffix}"
    )


async def _fetch_google_subscription(purchase_token: str) -> dict[str, Any]:
    access_token = await _get_access_token()
    try:
        async with build_async_client(timeout=15.0) as client:
            response = await client.get(
                _subscription_url(purchase_token),
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail="连接 Google Play Developer API 失败",
        ) from exc

    if response.status_code in (400, 404, 410):
        raise StoreVerificationError(
            "invalid_receipt",
            http_status=400,
            detail="Google Play 找不到该购买凭证",
        )
    if response.status_code in (401, 403):
        logger.error(
            "Google Play API 鉴权失败，请检查服务账号权限: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail="Google Play API 鉴权失败",
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail=f"Google Play API 暂时不可用: {response.status_code}",
        )
    if response.status_code >= 400:
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail=f"Google Play API 返回异常: {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail="Google Play API 返回了非 JSON 内容",
        ) from exc
    if not isinstance(payload, dict):
        raise StoreVerificationError(
            "store_unavailable",
            http_status=503,
            detail="Google Play API 返回格式异常",
        )
    return payload


async def _acknowledge_google_subscription(purchase_token: str) -> None:
    """确认购买。失败只记日志，让下次刷新再试，不影响本次授权。"""
    access_token = await _get_access_token()
    try:
        async with build_async_client(timeout=15.0) as client:
            response = await client.post(
                _subscription_url(purchase_token, acknowledge=True),
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Google Play acknowledge 请求失败: %s", exc)
        return

    # 409 表示已经被确认过，和成功等价。
    if response.status_code >= 400 and response.status_code != 409:
        logger.warning(
            "Google Play acknowledge 失败: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )


def parse_google_subscription(
    payload: dict[str, Any],
    *,
    expected_product_id: str,
    purchase_token: str,
    now: datetime | None = None,
) -> VerifiedSubscription:
    """把 subscriptionsv2 响应转成内部订阅快照。

    只认 Google 返回的到期时间；客户端传什么都不参与判断。
    """
    current = now or datetime.now(UTC)
    state = str(payload.get("subscriptionState") or "")
    if state not in _ENTITLED_STATES:
        raise StoreVerificationError(
            "not_active",
            http_status=400,
            detail=f"订阅状态不可用: {state or 'UNKNOWN'}",
        )

    line_items = payload.get("lineItems") or []
    matching = [
        item
        for item in line_items
        if isinstance(item, dict)
        and item.get("productId") == expected_product_id
    ]
    if not matching:
        raise StoreVerificationError(
            "product_mismatch",
            http_status=400,
            detail="Google Play 返回的商品与请求不一致",
        )

    expiries: list[datetime] = []
    for item in matching:
        expiry = item.get("expiryTime")
        if not expiry:
            continue
        try:
            expiries.append(_parse_time(str(expiry)))
        except ValueError as exc:
            raise StoreVerificationError(
                "invalid_receipt",
                http_status=400,
                detail="Google Play 返回的到期时间格式错误",
            ) from exc

    if not expiries:
        raise StoreVerificationError(
            "invalid_receipt",
            http_status=400,
            detail="Google Play 未返回订阅到期时间",
        )

    expires_at = max(expiries)
    if expires_at <= current:
        raise StoreVerificationError(
            "not_active",
            http_status=400,
            detail="订阅已过期",
        )

    auto_renewing = any(
        bool((item.get("autoRenewingPlan") or {}).get("autoRenewEnabled"))
        for item in matching
    )
    return VerifiedSubscription(
        platform="android",
        product_id=expected_product_id,
        purchase_token=purchase_token,
        transaction_id=payload.get("latestOrderId") or None,
        expires_at=expires_at,
        auto_renewing=auto_renewing,
        store_state=state,
    )


async def verify_google_play_subscription(
    *,
    purchase_token: str,
    expected_product_id: str,
) -> VerifiedSubscription:
    token = (purchase_token or "").strip()
    if not token:
        raise StoreVerificationError(
            "receipt_required",
            http_status=400,
            detail="缺少 Google Play purchaseToken",
        )

    payload = await _fetch_google_subscription(token)
    verified = parse_google_subscription(
        payload,
        expected_product_id=expected_product_id,
        purchase_token=token,
    )

    if payload.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING":
        await _acknowledge_google_subscription(token)

    logger.info(
        "Google Play 订阅校验通过: product=%s state=%s expires=%s",
        verified.product_id,
        verified.store_state,
        verified.expires_at.isoformat(),
    )
    return verified


async def verify_store_subscription(
    *,
    platform: str,
    product_id: str,
    purchase_token: str | None,
    transaction_id: str | None,
) -> VerifiedSubscription:
    """按平台向商店校验。没有实现的平台一律拒绝。"""
    if platform == "android":
        return await verify_google_play_subscription(
            purchase_token=purchase_token or "",
            expected_product_id=product_id,
        )
    if platform == "ios":
        raise StoreVerificationError(
            "platform_unsupported",
            http_status=400,
            detail="iOS 订阅服务端校验尚未接入",
        )
    raise StoreVerificationError(
        "platform_unsupported",
        http_status=400,
        detail=f"不支持的订阅平台: {platform}",
    )
