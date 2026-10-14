import pytest
from openai import OpenAIError

from app.workflows import record_workflow as record_module
from app.workflows import search_workflow as search_module


@pytest.mark.asyncio
async def test_execute_record_workflow_returns_record_state(monkeypatch):
    record_id = "11111111-1111-1111-1111-111111111111"

    class FakeWorkflow:
        async def ainvoke(self, initial_state):
            return {
                **initial_state.__dict__,
                "summary": "hello",
                "record_id": record_id,
            }

    monkeypatch.setattr(record_module, "record_workflow", FakeWorkflow())

    result = await record_module.execute_record_workflow(
        user_id="00000000-0000-0000-0000-000000000001",
        conversation_id="22222222-2222-2222-2222-222222222222",
        content_type="text",
        content="hello",
    )

    assert isinstance(result, record_module.RecordState)
    assert result.record_id == record_id
    assert result.summary == "hello"


@pytest.mark.asyncio
async def test_execute_search_workflow_returns_search_state(monkeypatch):
    class FakeWorkflow:
        async def ainvoke(self, initial_state):
            return {
                **initial_state.__dict__,
                "answer": "matched",
                "results": [{"record_id": "11111111-1111-1111-1111-111111111111"}],
            }

    monkeypatch.setattr(search_module, "search_workflow", FakeWorkflow())

    result = await search_module.execute_search_workflow(
        user_id="00000000-0000-0000-0000-000000000001",
        conversation_id="22222222-2222-2222-2222-222222222222",
        query="hello",
    )

    assert isinstance(result, search_module.SearchState)
    assert result.answer == "matched"
    assert result.results == [{"record_id": "11111111-1111-1111-1111-111111111111"}]


@pytest.mark.asyncio
async def test_embed_node_degrades_when_model_service_fails(monkeypatch):
    class FakeEmbeddingService:
        async def embed(self, text):
            raise OpenAIError("invalid api key")

    monkeypatch.setattr(record_module, "EmbeddingService", FakeEmbeddingService)

    state = record_module.RecordState(
        user_id="00000000-0000-0000-0000-000000000001",
        content_type="text",
        content="hello",
    )

    result = await record_module.embed_node(state)

    assert result["embedding_vector"] == []
    assert "向量化失败" in result["error"]


@pytest.mark.asyncio
async def test_process_voice_node_defers_transcription_without_text(monkeypatch):
    """没有转写文本时保存不再同步跑 ASR，交给后台任务。"""

    class FailingAsrService:
        def __init__(self):
            raise AssertionError("保存流程不应该同步调用 ASR")

    monkeypatch.setattr(record_module, "ASRService", FailingAsrService)

    state = record_module.RecordState(
        user_id="00000000-0000-0000-0000-000000000001",
        content_type="voice",
        content="",
    )

    result = await record_module.process_voice_node(state)

    assert result["content"] == ""
    assert result["transcription_status"] == "pending"


@pytest.mark.asyncio
async def test_process_voice_node_uses_client_transcript(monkeypatch):
    """实时转写的结果直接用，状态直接是完成。"""

    class FailingAsrService:
        def __init__(self):
            raise AssertionError("已有转写文本时不应该再调 ASR")

    monkeypatch.setattr(record_module, "ASRService", FailingAsrService)

    state = record_module.RecordState(
        user_id="00000000-0000-0000-0000-000000000001",
        content_type="voice",
        content="  今天的会议记录  ",
    )

    result = await record_module.process_voice_node(state)

    assert result["content"] == "今天的会议记录"
    assert result["transcription_status"] == "done"


@pytest.mark.asyncio
async def test_embed_node_skips_empty_content(monkeypatch):
    """转写未完成时 content 为空，不应调用向量化服务。"""

    class FailingEmbeddingService:
        def __init__(self):
            raise AssertionError("空内容不应该调用向量化服务")

    monkeypatch.setattr(record_module, "EmbeddingService", FailingEmbeddingService)

    state = record_module.RecordState(
        user_id="00000000-0000-0000-0000-000000000001",
        content_type="voice",
        content="",
        transcription_status="pending",
    )

    result = await record_module.embed_node(state)

    assert result["embedding_vector"] == []


class _FakeRecord:
    """只记录字段变化，不碰真实数据库。"""

    def __init__(self):
        self.content = ""
        self.summary = None
        self.transcription_status = "pending"
        self.vector_id = None
        self.updated_at = None


class _FakeSession:
    def __init__(self, record):
        self._record = record
        self.committed = False

    async def get(self, _model, _key):
        return self._record

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


def _patch_voice_pipeline(monkeypatch, *, transcript, summary):
    """把后台转写依赖的服务全部换成假实现。"""

    class FakeAsr:
        async def transcribe_with_summary(self, _data, _filename):
            if isinstance(transcript, Exception):
                raise transcript
            return {"text": transcript, "summary": summary}

    class FakeEmbedding:
        async def embed(self, _text):
            return [0.1, 0.2]

    class FakeVectorClient:
        def upsert(self, **_kwargs):
            return "vector-id"

    monkeypatch.setattr(record_module, "ASRService", FakeAsr)
    monkeypatch.setattr(record_module, "EmbeddingService", FakeEmbedding)
    monkeypatch.setattr(record_module, "get_vector_client", FakeVectorClient)


@pytest.mark.asyncio
async def test_background_transcription_writes_back(monkeypatch):
    _patch_voice_pipeline(monkeypatch, transcript="明天十点评审会", summary="评审会")
    record = _FakeRecord()
    session = _FakeSession(record)
    monkeypatch.setattr(
        record_module, "async_session_factory", _FakeSessionFactory(session)
    )

    await record_module.transcribe_voice_record(
        record_id="11111111-1111-1111-1111-111111111111",
        user_id="00000000-0000-0000-0000-000000000001",
        file_data=b"audio",
        filename="audio.wav",
    )

    assert record.content == "明天十点评审会"
    assert record.summary == "评审会"
    assert record.transcription_status == "done"
    assert record.vector_id == "vector-id"
    assert record.updated_at is not None
    assert session.committed is True


@pytest.mark.asyncio
async def test_background_transcription_failure_keeps_record(monkeypatch):
    from app.services.asr_service import ASRServiceError

    _patch_voice_pipeline(
        monkeypatch, transcript=ASRServiceError("转写服务不可用"), summary=None
    )
    record = _FakeRecord()
    session = _FakeSession(record)
    monkeypatch.setattr(
        record_module, "async_session_factory", _FakeSessionFactory(session)
    )

    await record_module.transcribe_voice_record(
        record_id="11111111-1111-1111-1111-111111111111",
        user_id="00000000-0000-0000-0000-000000000001",
        file_data=b"audio",
        filename="audio.wav",
    )

    assert record.transcription_status == "failed"
    assert record.vector_id is None
    assert record.updated_at is not None
    assert session.committed is True
