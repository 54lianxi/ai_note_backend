"""笔记标题编辑的单元测试。"""

import uuid
from datetime import UTC, datetime

import pytest

from app.api import record as record_api
from app.api.record import _embedding_text, update_record
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


class FakeVectorClient:
    def replace_chunks(self, chunks, record_id, user_id):
        # chunks 是 embedding 输出，记录下送进向量的文本便于断言
        FakeVectorClient.last_chunks = chunks
        return "vec-1"


FakeVectorClient.last_chunks = []


def _user_id():
    return str(uuid.uuid4())


@pytest.fixture
def fake_embedding(monkeypatch):
    """替换 EmbeddingService 构造与 embed_text_chunks，记录向量化文本。"""
    monkeypatch.setattr(record_api, "EmbeddingService", lambda: object())
    embedded_texts: list[str] = []

    async def fake_embed(_svc, text):
        embedded_texts.append(text)
        return [{"text": text, "vector": [0.1, 0.2]}]

    monkeypatch.setattr(record_api, "embed_text_chunks", fake_embed)
    monkeypatch.setattr(record_api, "get_vector_client", FakeVectorClient)
    return embedded_texts


@pytest.mark.asyncio
async def test_title_only_patch_updates_title_and_reindexes(fake_embedding):
    """列表里仅改标题：标题更新、内容/摘要不动，且向量以标题+正文重建。"""
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
    assert record.vector_id == "vec-1"
    assert fake_embedding == ["我的标题\n\n原始内容"]
    assert FakeVectorClient.last_chunks[0]["text"] == "我的标题\n\n原始内容"
    assert db.flushed


@pytest.mark.asyncio
async def test_empty_title_clears_custom_title_and_reindexes(fake_embedding):
    """标题改成空白等于清除自定义标题，向量回落到纯正文。"""
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
    assert fake_embedding == ["原始内容"]


@pytest.mark.asyncio
async def test_content_patch_still_updates_content_and_vector(fake_embedding):
    """带 content 的编辑：内容、摘要照旧更新，向量文本为标题+正文。"""
    record = FakeRecord()
    db = FakeDb(record)

    response = await update_record(
        record.id,
        RecordUpdate(content="编辑后的内容", title="新标题"),
        db,
        _user_id(),
        None,
    )

    assert response.title == "新标题"
    assert record.content == "编辑后的内容"
    assert record.summary == "编辑后的内容"
    assert record.vector_id == "vec-1"
    assert fake_embedding == ["新标题\n\n编辑后的内容"]


def test_embedding_text_without_title_is_plain_content():
    assert _embedding_text(None, "正文") == "正文"
    assert _embedding_text("  ", "正文") == "正文"
    assert _embedding_text("标题", "正文") == "标题\n\n正文"
