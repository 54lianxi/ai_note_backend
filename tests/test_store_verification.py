"""Google Play 订阅服务端校验的单元测试。"""
from datetime import UTC, datetime, timedelta

import pytest

from app.schemas.subscription import VerifySubscriptionRequest
from app.services import store_verification
from app.services.store_verification import (
    StoreVerificationError,
    parse_google_subscription,
)

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
PRODUCT_ID = "note_sub_month_2.99"


def _payload(
    *,
    state: str = "SUBSCRIPTION_STATE_ACTIVE",
    product_id: str = PRODUCT_ID,
    expires_in: timedelta = timedelta(days=30),
    auto_renew: bool = True,
    acknowledgement: str = "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
) -> dict:
    expiry = (NOW + expires_in).isoformat().replace("+00:00", "Z")
    return {
        "subscriptionState": state,
        "acknowledgementState": acknowledgement,
        "latestOrderId": "GPA.1234-5678-9012",
        "lineItems": [
            {
                "productId": product_id,
                "expiryTime": expiry,
                "autoRenewingPlan": {"autoRenewEnabled": auto_renew},
            }
        ],
    }


def _parse(payload: dict):
    return parse_google_subscription(
        payload,
        expected_product_id=PRODUCT_ID,
        purchase_token="purchase-token",
        now=NOW,
    )


def test_active_subscription_is_verified_with_store_expiry():
    verified = _parse(_payload())

    assert verified.platform == "android"
    assert verified.product_id == PRODUCT_ID
    assert verified.purchase_token == "purchase-token"
    assert verified.transaction_id == "GPA.1234-5678-9012"
    assert verified.expires_at == NOW + timedelta(days=30)
    assert verified.auto_renewing is True
    assert verified.store_state == "SUBSCRIPTION_STATE_ACTIVE"


@pytest.mark.parametrize(
    "state",
    [
        "SUBSCRIPTION_STATE_CANCELED",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    ],
)
def test_canceled_and_grace_period_still_grant_access(state):
    verified = _parse(_payload(state=state))
    assert verified.expires_at > NOW


@pytest.mark.parametrize(
    "state",
    [
        "SUBSCRIPTION_STATE_EXPIRED",
        "SUBSCRIPTION_STATE_ON_HOLD",
        "SUBSCRIPTION_STATE_PAUSED",
        "SUBSCRIPTION_STATE_PENDING",
    ],
)
def test_non_entitled_states_are_rejected(state):
    with pytest.raises(StoreVerificationError) as exc:
        _parse(_payload(state=state))
    assert exc.value.code == "not_active"


def test_product_mismatch_is_rejected():
    with pytest.raises(StoreVerificationError) as exc:
        _parse(_payload(product_id="another_product"))
    assert exc.value.code == "product_mismatch"


def test_expired_expiry_time_is_rejected():
    with pytest.raises(StoreVerificationError) as exc:
        _parse(_payload(expires_in=timedelta(minutes=-1)))
    assert exc.value.code == "not_active"


def test_missing_expiry_is_rejected():
    payload = _payload()
    payload["lineItems"][0].pop("expiryTime")

    with pytest.raises(StoreVerificationError) as exc:
        _parse(payload)
    assert exc.value.code == "invalid_receipt"


def test_client_cannot_supply_entitlement_fields():
    request = VerifySubscriptionRequest(
        platform="android",
        product_id=PRODUCT_ID,
        purchase_token="purchase-token",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        auto_renewing=True,
    )

    assert not hasattr(request, "expires_at")
    assert not hasattr(request, "auto_renewing")


@pytest.mark.asyncio
async def test_verify_fetches_google_and_acknowledges_pending_purchase(
    monkeypatch,
):
    acknowledged: list[str] = []

    async def fake_fetch(token: str) -> dict:
        assert token == "purchase-token"
        return _payload(acknowledgement="ACKNOWLEDGEMENT_STATE_PENDING")

    async def fake_acknowledge(token: str) -> None:
        acknowledged.append(token)

    monkeypatch.setattr(
        store_verification, "_fetch_google_subscription", fake_fetch
    )
    monkeypatch.setattr(
        store_verification,
        "_acknowledge_google_subscription",
        fake_acknowledge,
    )

    verified = await store_verification.verify_google_play_subscription(
        purchase_token="purchase-token",
        expected_product_id=PRODUCT_ID,
    )

    assert verified.product_id == PRODUCT_ID
    assert acknowledged == ["purchase-token"]


@pytest.mark.asyncio
async def test_ios_is_rejected_until_app_store_server_api_is_configured():
    with pytest.raises(StoreVerificationError) as exc:
        await store_verification.verify_store_subscription(
            platform="ios",
            product_id=PRODUCT_ID,
            purchase_token=None,
            transaction_id="1000000000000001",
        )
    assert exc.value.code == "platform_unsupported"
