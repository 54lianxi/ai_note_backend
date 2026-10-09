from types import SimpleNamespace

import pytest

from app.services.llm_service import LLMService


class FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


def make_service(content: str):
    completions = FakeCompletions(content)
    service = LLMService.__new__(LLMService)
    service.model = "qwen-plus"
    service.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    return service, completions


@pytest.mark.asyncio
async def test_classify_text_intent_returns_search_from_model_json():
    service, completions = make_service('{"intent":"search"}')

    intent = await service.classify_text_intent("海贼王看到第几集")

    assert intent == "search"
    assert completions.calls[0]["temperature"] == 0
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_classify_text_intent_returns_record_from_model_json():
    service, _ = make_service('{"intent":"record"}')

    intent = await service.classify_text_intent("海贼王看到了600集")

    assert intent == "record"


@pytest.mark.asyncio
async def test_classify_text_intent_falls_back_when_json_is_invalid():
    service, _ = make_service("search")

    intent = await service.classify_text_intent("海贼王看到第几集")

    assert intent == "search"
