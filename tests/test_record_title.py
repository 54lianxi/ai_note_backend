"""笔记标题编辑的单元测试。"""

import uuid
from datetime import datetime, UTC

import pytest

from app.api import record as record_api
from app.api.record import update_record
from app.schemas.record import RecordUpdate


class FakeRecord:
    def __init__(self, title=None):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.title = title
        self.content = "原始内容"
        self.summary = "原始摘要"
        self.content_type = "text"
        self.image_url = None
        self.image_key_info = None
        self.vector_id = None
        self.transcription_status = "done"
        self.transcription_error = None
        self.transcription_heartbeat_at = None
        self.manual_transcription_count = 0
        self.file_url = None
        self.file_name = None
        self.file_content_type = None
        self.file_size = None
        self.audio_duration_seconds = 0
        self.created_at = datetime.now(UTC)
        self.updated_at = None


class FakeResult:
    def __init__(self, record):
        self._record = record

    def scalar_one_or_none(self):
        return self._record


class FakeDb:
    def __init__(self, record):
        self.record = record
        self.flushed = False

    async def execute(self, _query):
        return FakeResult(self.record)

    async def flush(self):
        self.flushed = True


class _RaisingEmbedding:
    def __init__(self):
        raise AssertionError("仅改标题不应触发向量化")


async def fake_embed(_svc, _text):
    return [("chunk", [0.1, 0.2])]


class FakeVectorClient:
    def replace_chunks(self, chunks, record_id, user_id):
        return "vec-1"


def _user_id():
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_title_only_patch_updates_title_and_skips_vector(monkeypatch):
    """列表里仅改标题：标题更新，内容/摘要不动，不触发向量化。"""
    monkeypatch.setattr(record_api, "EmbeddingService", _RaisingEmbedding)
    record = FakeRecord()
    db = FakeDb(record)

    response = await update_record(
        record.id,
        RecordUpdate(title="我的标题"),
        db,
        _user_id(),
        None,
    )

    assert response.title == "我的标题"
    assert record.title == "我的标题"
    assert record.content == "原始内容"
    assert record.summary == "原始摘要"
    assert db.flushed


@pytest.mark.asyncio
async def test_empty_title_clears_custom_title(monkeypatch):
    """标题改成空白等于清除自定义标题，回落到内容展示。"""
    monkeypatch.setattr(record_api, "EmbeddingService", _RaisingEmbedding)
    record = FakeRecord(title="旧标题")
    db = FakeDb(record)

    response = await update_record(
        record.id,
        RecordUpdate(title="   "),
        db,
        _user_id(),
        None,
    )

    assert response.title is None
    assert record.title is None


@pytest.mark.asyncio
async def test_content_patch_still_updates_content_and_vector(monkeypatch):
    """带 content 的编辑：内容、摘要、向量照旧更新，标题也可一并带上。"""
    monkeypatch.setattr(record_api, "embed_text_chunks", fake_embed)
    monkeypatch.setattr(record_api, "get_vector_client", lambda: FakeVectorClient())
    record = FakeRecord()
    db = FakeDb(record)

    response = await update_record(
        record.id,
        RecordUpdate(content="编辑后的内容", title="新标题"),
        db,
        _user_id(),
        None,
    )

    assert record.content == "编辑后的内容"
    assert record.summary == "编辑后的内容"
    assert record.title == "新标题"
    assert record.vector_id == "vec-1"
    assert response.title == "新标题"
