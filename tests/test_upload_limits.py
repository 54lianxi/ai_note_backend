import uuid

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks

from app.api import record as record_api
from app.api.record import _read_upload
from app.config import settings
from app.services.subscription_service import (
    FREE_FILE_NOTE_LIMIT,
    FREE_IMAGE_NOTE_LIMIT,
    FREE_VOICE_NOTE_LIMIT,
    SubscriptionState,
    plan_voice_note_max_seconds,
)


class _FakeUpload:
    def __init__(self, data: bytes, size: int | None):
        self.data = data
        self.size = size
        self.was_read = False
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        self.was_read = True
        if size < 0:
            chunk = self.data[self.offset :]
        else:
            chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_read_upload_rejects_known_oversized_file_before_reading():
    file = _FakeUpload(data=b"x", size=1024 * 1024 + 1)

    with pytest.raises(HTTPException) as error:
        await _read_upload(file, max_mb=1, language="zh")

    assert error.value.status_code == 413
    assert "1 MB" in error.value.detail
    assert file.was_read is False


@pytest.mark.asyncio
async def test_read_upload_rejects_oversized_stream_without_reading_it_all():
    file = _FakeUpload(data=b"x" * (1024 * 1024 + 1), size=None)

    with pytest.raises(HTTPException) as error:
        await _read_upload(file, max_mb=1, language="en")

    assert error.value.status_code == 413
    assert "1 MB" in error.value.detail


@pytest.mark.asyncio
async def test_read_upload_returns_file_at_limit():
    data = b"x" * (1024 * 1024)

    assert await _read_upload(_FakeUpload(data, size=None), 1, "zh") == data


class _FakeAudioFile:
    content_type = "audio/wav"


class _PassedDurationGate(Exception):
    """标记流程已经走过时长校验，进入上传落盘阶段。"""


def _state_for(is_subscribed: bool):
    return SubscriptionState(
        is_subscribed=is_subscribed,
        product_id=None,
        platform=None,
        expires_at=None,
        auto_renewing=False,
        voice_note_count=0,
        voice_note_limit=None if is_subscribed else FREE_VOICE_NOTE_LIMIT,
        image_note_count=0,
        image_note_limit=None if is_subscribed else FREE_IMAGE_NOTE_LIMIT,
        file_note_count=0,
        file_note_limit=None if is_subscribed else FREE_FILE_NOTE_LIMIT,
        voice_note_max_seconds=plan_voice_note_max_seconds(is_subscribed),
    )


async def _save_voice(monkeypatch, *, is_subscribed: bool, duration_seconds: int):
    async def fake_get_state(*_args, **_kwargs):
        return _state_for(is_subscribed)

    async def fake_spool(*_args, **_kwargs):
        raise _PassedDurationGate

    monkeypatch.setattr(record_api, "get_state", fake_get_state)
    monkeypatch.setattr(record_api, "_spool_voice_upload", fake_spool)
    return await record_api.create_voice_record(
        _FakeAudioFile(),
        None,
        str(uuid.uuid4()),
        "zh",
        BackgroundTasks(),
        duration_seconds=duration_seconds,
    )


@pytest.mark.asyncio
async def test_voice_note_over_plan_cap_is_rejected(monkeypatch):
    """免费版单条超过 10 分钟：保存请求直接 402，不再靠客户端自觉。"""
    with pytest.raises(HTTPException) as error:
        await _save_voice(monkeypatch, is_subscribed=False, duration_seconds=10 * 60 + 1)

    assert error.value.status_code == 402
    assert str(settings.SUBSCRIPTION_FREE_VOICE_MAX_MINUTES) in error.value.detail


@pytest.mark.asyncio
async def test_subscription_allows_ten_minute_voice_note(monkeypatch):
    """同样的 11 分钟笔记，订阅用户不该被时长拦住。"""
    with pytest.raises(_PassedDurationGate):
        await _save_voice(monkeypatch, is_subscribed=True, duration_seconds=11 * 60)
