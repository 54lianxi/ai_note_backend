import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTasks

from app.api import record as record_api
from app.models.record import Record


class _Result:
    def __init__(self, record):
        self.record = record

    def scalar_one_or_none(self):
        return self.record


class _Db:
    def __init__(self, record):
        self.record = record
        self.committed = False

    async def execute(self, _statement):
        return _Result(self.record)

    async def commit(self):
        self.committed = True


class _Response:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self):
        return self.data

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


class _Minio:
    bucket = "records"

    def __init__(self):
        self.client = self
        self.response = None

    def get_object(self, _bucket, _name):
        self.response = _Response(b"audio")
        return self.response


def _record(*, count: int = 0) -> Record:
    return Record(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        content="",
        summary=None,
        content_type="voice",
        transcription_status="failed",
        manual_transcription_count=count,
        file_url="http://localhost:9000/records/records/voice.wav",
        file_name="voice.wav",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_manual_transcription_requires_pro(monkeypatch):
    async def not_subscribed(*_args):
        return False

    monkeypatch.setattr(record_api, "is_subscribed", not_subscribed)

    with pytest.raises(HTTPException) as error:
        await record_api.retry_transcription(
            _record().id,
            _Db(_record()),
            str(uuid.uuid4()),
            "zh",
            BackgroundTasks(),
        )

    assert error.value.status_code == 402


@pytest.mark.asyncio
async def test_manual_transcription_schedules_background_retry(monkeypatch):
    async def subscribed(*_args):
        return True

    record = _record()
    db = _Db(record)
    minio = _Minio()
    background = BackgroundTasks()
    monkeypatch.setattr(record_api, "is_subscribed", subscribed)
    monkeypatch.setattr(record_api, "get_minio_client", lambda: minio)

    response = await record_api.retry_transcription(
        record.id,
        db,
        str(record.user_id),
        "zh",
        background,
    )

    assert response.transcription_status == "pending"
    assert record.manual_transcription_count == 1
    assert db.committed is True
    assert minio.response.closed is True
    assert len(background.tasks) == 1


@pytest.mark.asyncio
async def test_manual_transcription_limit_is_enforced(monkeypatch):
    async def subscribed(*_args):
        return True

    monkeypatch.setattr(record_api, "is_subscribed", subscribed)

    with pytest.raises(HTTPException) as error:
        await record_api.retry_transcription(
            _record(count=record_api.settings.MANUAL_TRANSCRIPTION_LIMIT).id,
            _Db(
                _record(
                    count=record_api.settings.MANUAL_TRANSCRIPTION_LIMIT
                )
            ),
            str(uuid.uuid4()),
            "zh",
            BackgroundTasks(),
        )

    assert error.value.status_code == 429
