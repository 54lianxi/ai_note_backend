"""订阅额度与文案的单元测试。"""
from app.config import settings
from app.core.i18n import t
from app.services.subscription_service import (
    FREE_VOICE_NOTE_LIMIT,
    SubscriptionState,
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
    }
    base.update(overrides)
    return SubscriptionState(**base)


def test_free_limit_matches_config():
    assert FREE_VOICE_NOTE_LIMIT == settings.SUBSCRIPTION_FREE_VOICE_LIMIT == 5


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
