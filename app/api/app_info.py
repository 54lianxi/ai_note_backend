"""客户端版本检测与语言包接口。"""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.services.llm_service import LLMService

router = APIRouter(tags=["App"])
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_LANGUAGE_PACK_DIR = _STATIC_DIR / "language_packs"
_SUPPORTED_LANGUAGES = {
    "en",
    "zh",
    "es",
    "fr",
    "de",
    "pt",
    "it",
    "ja",
    "ko",
    "ru",
    "ar",
    "hi",
    "id",
    "tr",
    "vi",
    "th",
}
_LANGUAGE_PACK_OVERRIDES = {
    "zh": {"thinking": "正在思考..."},
    "es": {"thinking": "Pensando..."},
    "fr": {"thinking": "Réflexion..."},
    "de": {"thinking": "Denkt nach..."},
    "pt": {"thinking": "Pensando..."},
    "it": {"thinking": "Sto pensando..."},
    "ja": {"thinking": "考え中..."},
    "ko": {"thinking": "생각 중..."},
    "ru": {"thinking": "Думаю..."},
    "ar": {"thinking": "جارٍ التفكير..."},
    "hi": {"thinking": "सोच रहा है..."},
    "id": {"thinking": "Sedang berpikir..."},
    "tr": {"thinking": "Düşünüyor..."},
    "vi": {"thinking": "Đang suy nghĩ..."},
    "th": {"thinking": "กำลังคิด..."},
}


class AppVersionResponse(BaseModel):
    """最新版本信息，客户端拿它和自己当前版本比较。"""

    latest_version: str
    min_version: str
    download_url: str
    note: str


@router.get("/app/version", response_model=AppVersionResponse)
async def get_app_version() -> AppVersionResponse:
    """客户端启动时检查更新，无需登录。"""
    return AppVersionResponse(
        latest_version=settings.APP_LATEST_VERSION,
        min_version=settings.APP_MIN_VERSION,
        download_url=settings.APP_UPDATE_URL,
        note=settings.APP_UPDATE_NOTE,
    )


@router.get("/app/language-packs/{language}")
async def get_language_pack(language: str):
    """返回按需翻译并缓存的 UI 语言包，客户端只在首次下载。"""
    code = language.lower()
    if code not in _SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=404, detail="Unsupported language")

    static_file = _LANGUAGE_PACK_DIR / f"{code}.json"
    if static_file.is_file():
        pack = json.loads(static_file.read_text("utf-8"))
        pack.update(_LANGUAGE_PACK_OVERRIDES.get(code, {}))
        return JSONResponse(
            pack,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    cache_file = Path(settings.LANGUAGE_PACK_CACHE_DIR) / f"{code}.json"
    if not cache_file.is_file():
        source = json.loads((_LANGUAGE_PACK_DIR / "en.json").read_text("utf-8"))
        try:
            translated = await LLMService().translate_language_pack(source, code)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Language pack is temporarily unavailable",
            ) from exc
        pack = {key: translated[key] for key in source}

        def write_cache() -> None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(pack, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

        await asyncio.to_thread(write_cache)

    pack = json.loads(cache_file.read_text("utf-8"))
    pack.update(_LANGUAGE_PACK_OVERRIDES.get(code, {}))
    return JSONResponse(
        pack,
        headers={"Cache-Control": "public, max-age=86400"},
    )
