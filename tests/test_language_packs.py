import json

import pytest

from app.api import app_info


@pytest.mark.asyncio
async def test_static_language_pack_is_complete():
    response = await app_info.get_language_pack("zh")
    pack = json.loads(response.body)

    assert pack["homeTitle"]
    assert pack["thinking"] == "正在思考..."
    assert len(pack) > 300


@pytest.mark.asyncio
async def test_generated_language_pack_is_cached(monkeypatch, tmp_path):
    source = {"home": "Home", "upload": "Limit {limit} MB"}
    monkeypatch.setattr(
        app_info,
        "LLMService",
        lambda: _FakeTranslationService(source),
    )
    monkeypatch.setattr(
        app_info.settings,
        "LANGUAGE_PACK_CACHE_DIR",
        str(tmp_path),
    )

    response = await app_info.get_language_pack("es")

    assert json.loads(response.body)["navHome"] == "Inicio"
    assert (tmp_path / "es.json").is_file()


class _FakeTranslationService:
    def __init__(self, source):
        self.source = source

    async def translate_language_pack(self, source, language):
        assert language == "es"
        translated = {key: value for key, value in source.items()}
        translated.update(
            {"navHome": "Inicio", "uploadImageTooLarge": "Máximo {limit} MB"}
        )
        return translated
