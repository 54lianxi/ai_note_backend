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
