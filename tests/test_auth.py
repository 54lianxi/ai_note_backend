import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.api.auth import (
    _apple_user_id,
    _apple_username,
    _google_user_id,
    _google_username,
)
from app.config import settings
from app.core.security import (
    DEV_ACCESS_TOKEN,
    DEV_USER_ID,
    create_access_token,
    decode_access_token,
    get_current_user_id,
)


def test_apple_user_id_is_stable():
    apple_sub = "001234.abcdef"

    assert _apple_user_id(apple_sub) == _apple_user_id(apple_sub)
    assert _apple_user_id(apple_sub) != _apple_user_id("different-sub")


def test_apple_username_fits_existing_column():
    username = _apple_username("001234.abcdef")

    assert username.startswith("apple_")
    assert len(username) <= 50


def test_google_user_id_is_stable():
    google_sub = "google-sub-123"

    assert _google_user_id(google_sub) == _google_user_id(google_sub)
    assert _google_user_id(google_sub) != _google_user_id("different-sub")


def test_google_username_fits_existing_column():
    username = _google_username("google-sub-123")

    assert username.startswith("google_")
    assert len(username) <= 50


def test_access_token_contains_user_subject():
    user_id = str(_apple_user_id("001234.abcdef"))

    token = create_access_token({"sub": user_id})
    payload = decode_access_token(token)

    assert payload is not None
    assert payload["sub"] == user_id


def test_dev_auth_bypass_requires_debug_and_enabled_flag(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=DEV_ACCESS_TOKEN,
    )

    assert get_current_user_id(credentials) == DEV_USER_ID


def test_dev_auth_bypass_is_rejected_when_debug_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=DEV_ACCESS_TOKEN,
    )

    with pytest.raises(HTTPException) as error:
        get_current_user_id(credentials)

    assert error.value.status_code == 401
