import json
import re

import pytest

from app.api import app_info

_PACK_PATHS = sorted(app_info._LANGUAGE_PACK_DIR.glob("*.json"))


@pytest.mark.asyncio
async def test_static_language_pack_is_complete():
    response = await app_info.get_language_pack("zh")
    pack = json.loads(response.body)

    assert pack["homeTitle"]
    assert pack["thinking"] == "正在思考..."
    assert len(pack) > 300


@pytest.mark.parametrize("path", _PACK_PATHS, ids=lambda path: path.stem)
def test_static_language_packs_match_base(path):
    en = json.loads((app_info._LANGUAGE_PACK_DIR / "en.json").read_text("utf-8"))
    pack = json.loads(path.read_text("utf-8"))

    assert list(pack) == list(en)
    for key, source in en.items():
        assert set(re.findall(r"\{\w+\}", source)) == set(
            re.findall(r"\{\w+\}", pack[key])
        ), key


@pytest.mark.asyncio
async def test_generated_language_pack_is_cached(monkeypatch, tmp_path):
    # 现在每种受支持语言都有静态包，先挪开静态目录才能测到运行时翻译。
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "en.json").write_text(
        json.dumps({"navHome": "Home", "upload": "Limit {limit} MB"}),
        encoding="utf-8",
    )
    cache_dir = tmp_path / "cache"
    source = {"home": "Home", "upload": "Limit {limit} MB"}
    monkeypatch.setattr(app_info, "_LANGUAGE_PACK_DIR", static_dir)
    monkeypatch.setattr(
        app_info,
        "LLMService",
        lambda: _FakeTranslationService(source),
    )
    monkeypatch.setattr(
        app_info.settings,
        "LANGUAGE_PACK_CACHE_DIR",
        str(cache_dir),
    )

    response = await app_info.get_language_pack("es")

    assert json.loads(response.body)["navHome"] == "Inicio"
    assert (cache_dir / "es.json").is_file()


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
