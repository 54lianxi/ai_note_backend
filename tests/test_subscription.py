"""订阅额度与文案的单元测试。"""
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.core.i18n import t
from app.models.subscription import Subscription
from app.services import subscription_service
from app.services.store_verification import (
    StoreVerificationError,
    VerifiedSubscription,
)
from app.services.subscription_service import (
    FREE_FILE_NOTE_LIMIT,
    FREE_IMAGE_NOTE_LIMIT,
    FREE_VOICE_NOTE_LIMIT,
    SubscriptionState,
    _is_active,
    refresh_subscription_if_stale,
)


def _state(**overrides) -> SubscriptionState:
    base = {
        "is_subscribed": False,
        "product_id": None,
        "platform": None,
        "expires_at": None,
        "auto_renewing": False,
        "voice_note_count": 0,
        "voice_note_limit": FREE_VOICE_NOTE_LIMIT,
        "image_note_count": 0,
        "image_note_limit": FREE_IMAGE_NOTE_LIMIT,
        "file_note_count": 0,
        "file_note_limit": FREE_FILE_NOTE_LIMIT,
    }
    base.update(overrides)
    return SubscriptionState(**base)


def test_free_limit_matches_config():
    assert FREE_VOICE_NOTE_LIMIT == settings.SUBSCRIPTION_FREE_VOICE_LIMIT == 3
    assert FREE_IMAGE_NOTE_LIMIT == settings.SUBSCRIPTION_FREE_IMAGE_LIMIT == 3
    assert FREE_FILE_NOTE_LIMIT == settings.SUBSCRIPTION_FREE_FILE_LIMIT == 1


def test_free_user_can_create_until_reaching_limit():
    assert _state(voice_note_count=0).can_create_voice_note
    assert _state(voice_note_count=FREE_VOICE_NOTE_LIMIT - 1).can_create_voice_note
    assert not _state(voice_note_count=FREE_VOICE_NOTE_LIMIT).can_create_voice_note


def test_subscribed_user_has_no_limit():
    state = _state(
        is_subscribed=True,
        voice_note_limit=None,
        voice_note_count=FREE_VOICE_NOTE_LIMIT * 10,
    )
    assert state.can_create_voice_note
    assert state.can_create_image_note
    assert state.can_create_file_note


def test_image_and_document_limits_are_independent():
    state = _state(
        voice_note_count=FREE_VOICE_NOTE_LIMIT,
        image_note_count=FREE_IMAGE_NOTE_LIMIT - 1,
        file_note_count=FREE_FILE_NOTE_LIMIT,
    )
    assert not state.can_create_voice_note
    assert state.can_create_image_note
    assert not state.can_create_file_note


def test_subscription_has_two_product_ids():
    assert settings.SUBSCRIPTION_WEEKLY_PRODUCT_ID == "note_sub_week_0.99"
    assert settings.SUBSCRIPTION_MONTHLY_PRODUCT_ID == "note_sub_month_2.99"


def test_limit_message_includes_number():
    assert str(FREE_VOICE_NOTE_LIMIT) in t(
        "zh", "subscription_rate_limit", limit=FREE_VOICE_NOTE_LIMIT
    )
    assert str(FREE_VOICE_NOTE_LIMIT) in t(
        "en", "subscription_rate_limit", limit=FREE_VOICE_NOTE_LIMIT
    )


def test_subscription_messages_exist_in_both_languages():
    for language in ("zh", "en"):
        assert t(language, "subscription_required")
        assert t(language, "subscription_product_unknown")
        assert t(language, "subscription_receipt_required")
        assert t(language, "subscription_receipt_invalid")
        assert t(language, "subscription_not_active")
        assert t(language, "subscription_verify_unavailable")
        assert t(language, "subscription_token_in_use")
        assert t(language, "subscription_platform_unsupported")


def _subscription(**overrides) -> Subscription:
    values = {
        "user_id": uuid.uuid4(),
        "product_id": "note_sub_month_2.99",
        "platform": "android",
        "purchase_token": "purchase-token",
        "status": "active",
        "expires_at": datetime.now(UTC) + timedelta(days=30),
        "last_verified_at": datetime.now(UTC),
    }
    values.update(overrides)
    return Subscription(**values)


def test_unverified_subscription_is_not_active(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription(last_verified_at=None)

    assert not _is_active(subscription, datetime.now(UTC))


def test_verified_subscription_is_active(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription()

    assert _is_active(subscription, datetime.now(UTC))


def test_verified_but_expired_subscription_is_not_active(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription(
        expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    assert not _is_active(subscription, datetime.now(UTC))


class _FakeDb:
    def __init__(self, subscription: Subscription):
        self.subscription = subscription

    async def execute(self, _statement):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.subscription
        return result


@pytest.mark.asyncio
async def test_refresh_revalidates_old_unverified_row(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription(last_verified_at=None)
    expires_at = datetime.now(UTC) + timedelta(days=20)

    async def fake_verify(**_kwargs):
        return VerifiedSubscription(
            platform="android",
            product_id=subscription.product_id,
            purchase_token=subscription.purchase_token,
            transaction_id="GPA.1234",
            expires_at=expires_at,
            auto_renewing=True,
            store_state="SUBSCRIPTION_STATE_ACTIVE",
        )

    monkeypatch.setattr(
        subscription_service, "verify_store_subscription", fake_verify
    )
    await refresh_subscription_if_stale(_FakeDb(subscription), subscription.user_id)

    assert subscription.last_verified_at is not None
    assert subscription.expires_at == expires_at
    assert _is_active(subscription, datetime.now(UTC))


@pytest.mark.asyncio
async def test_refresh_expires_invalid_old_row(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription(last_verified_at=None)

    async def fake_verify(**_kwargs):
        raise StoreVerificationError("invalid_receipt", detail="not found")

    monkeypatch.setattr(
        subscription_service, "verify_store_subscription", fake_verify
    )
    await refresh_subscription_if_stale(_FakeDb(subscription), subscription.user_id)

    assert subscription.status == "expired"
    assert subscription.last_verified_at is not None
    assert not _is_active(subscription, datetime.now(UTC))


@pytest.mark.asyncio
async def test_refresh_keeps_unverified_row_locked_when_store_is_down(monkeypatch):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    subscription = _subscription(last_verified_at=None)

    async def fake_verify(**_kwargs):
        raise StoreVerificationError("store_unavailable", http_status=503)

    monkeypatch.setattr(
        subscription_service, "verify_store_subscription", fake_verify
    )
    await refresh_subscription_if_stale(_FakeDb(subscription), subscription.user_id)

    assert subscription.last_verified_at is None
    assert not _is_active(subscription, datetime.now(UTC))


@pytest.mark.asyncio
async def test_refresh_keeps_previously_verified_row_when_store_is_down(
    monkeypatch,
):
    monkeypatch.setattr(settings, "SUBSCRIPTION_VERIFY_WITH_STORE", True)
    monkeypatch.setattr(settings, "SUBSCRIPTION_CACHE_TTL_SECONDS", 60)
    subscription = _subscription(
        last_verified_at=datetime.now(UTC) - timedelta(hours=2)
    )

    async def fake_verify(**_kwargs):
        raise StoreVerificationError("store_unavailable", http_status=503)

    monkeypatch.setattr(
        subscription_service, "verify_store_subscription", fake_verify
    )
    await refresh_subscription_if_stale(_FakeDb(subscription), subscription.user_id)

    assert _is_active(subscription, datetime.now(UTC))
