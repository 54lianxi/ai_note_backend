"""标题参与语义检索的端到端流程测试。

用真实的 split_text_chunks / embed_text_chunks / update_record 处理器，
配一个内存版余弦相似度向量库，验证：改标题前按标题词搜不到、
改标题后能搜到、且正文检索不受影响。
"""

import uuid
from datetime import UTC, datetime

import pytest

from app.api import record as record_api
from app.api.record import update_record
from app.schemas.record import RecordUpdate
from app.services.text_index import split_text_chunks
from app.workflows.record_workflow import embed_text_chunks

# 词表向量化：文本包含哪个词，对应维度就 +1。足够模拟语义匹配。
_VOCAB = ("苹果", "香蕉", "会议", "天气")

_USER_ID = str(uuid.uuid4())


class FakeEmbeddingService:
    async def embed_batch(self, texts, max_retries=5, task_type=None):
        return [_vectorize(t) for t in texts]


def _vectorize(text: str) -> list[float]:
    return [float(text.count(word)) for word in _VOCAB]


class FakeVectorStore:
    """内存版 Qdrant 替身：余弦相似度检索，payload 按 user_id 过滤。"""

    def __init__(self):
        self.points: dict[str, dict] = {}

    def replace_chunks(self, chunks, record_id, user_id):
        # 先删旧分块再写新的，模拟 VectorDBClient.replace_chunks
        self.delete_by_record(record_id)
        for index, chunk in enumerate(chunks):
            self.points[f"{record_id}:{index}"] = {
                "vector": chunk["vector"],
                "payload": {"record_id": record_id, "user_id": user_id},
            }
        return record_id

    def delete_by_record(self, record_id):
        for key in [k for k, v in self.points.items() if v["payload"]["record_id"] == record_id]:
            del self.points[key]

    def search(self, vector, user_id, limit=5):
        scored = []
        for point in self.points.values():
            if point["payload"]["user_id"] != user_id:
                continue
            score = _cosine(vector, point["vector"])
            if score > 0:
                scored.append((score, point["payload"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {"id": p["record_id"], "score": s, "payload": p}
            for s, p in scored[:limit]
        ]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeResult:
    def __init__(self, record):
        self._record = record

    def scalar_one_or_none(self):
        return self._record


class FakeDb:
    def __init__(self, record):
        self.record = record

    async def execute(self, _query):
        return FakeResult(self.record)

    async def flush(self):
        pass


class FakeRecord:
    def __init__(self, content):
        self.id = uuid.uuid4()
        self.user_id = uuid.UUID(_USER_ID)
        self.title = None
        self.content = content
        self.summary = None
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


async def _fake_query_vector(query: str) -> list[float]:
    return (await embed_text_chunks(FakeEmbeddingService(), query))[0]["vector"]


@pytest.mark.asyncio
async def test_renamed_title_is_searchable(monkeypatch):
    store = FakeVectorStore()
    monkeypatch.setattr(record_api, "EmbeddingService", FakeEmbeddingService)
    monkeypatch.setattr(record_api, "get_vector_client", lambda: store)

    record_a = FakeRecord("香蕉奶昔的做法")
    record_b = FakeRecord("本周会议纪要")
    # 模拟创建记录时按正文向量化（标题为空）
    store.replace_chunks(
        await embed_text_chunks(FakeEmbeddingService(), record_a.content),
        str(record_a.id),
        _USER_ID,
    )
    store.replace_chunks(
        await embed_text_chunks(FakeEmbeddingService(), record_b.content),
        str(record_b.id),
        _USER_ID,
    )

    # 改标题前：按「苹果」检索，两条都搜不到
    before = await _fake_query_vector("苹果")
    assert store.search(before, _USER_ID, 5) == []

    # 给 A 改标题「苹果购买清单」（走真实的 PATCH 处理器）
    response = await update_record(
        record_a.id,
        RecordUpdate(title="苹果购买清单"),
        FakeDb(record_a),
        _USER_ID,
        None,
    )
    assert response.title == "苹果购买清单"

    # 改标题后：按「苹果」检索命中 A，不命中 B
    hits = store.search(await _fake_query_vector("苹果"), _USER_ID, 5)
    hit_ids = {hit["payload"]["record_id"] for hit in hits}
    assert str(record_a.id) in hit_ids
    assert str(record_b.id) not in hit_ids

    # 正文检索不受影响：「会议」仍命中 B
    hits_b = store.search(await _fake_query_vector("会议"), _USER_ID, 5)
    hit_ids_b = {hit["payload"]["record_id"] for hit in hits_b}
    assert str(record_b.id) in hit_ids_b

    # 改标题的记录正文检索同样正常：「香蕉」命中 A
    hits_a = store.search(await _fake_query_vector("香蕉"), _USER_ID, 5)
    assert str(record_a.id) in {h["payload"]["record_id"] for h in hits_a}


def test_title_text_is_chunked_together_with_content():
    """标题拼接后送进真实分块逻辑，标题落在首个分块里。"""
    text = "跨年的会议安排\n\n" + "正文" * 1000
    chunks = split_text_chunks(text)
    assert chunks
    assert "跨年的会议安排" in chunks[0]
    # 分块可被 embed_text_chunks 正常向量化
    import asyncio

    chunks_embedded = asyncio.run(
        embed_text_chunks(FakeEmbeddingService(), text)
    )
    assert all(c["vector"] for c in chunks_embedded)
