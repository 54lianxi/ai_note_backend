"""ASR 语音识别服务"""
import base64
import io
import logging

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core.http import build_async_client

logger = logging.getLogger(__name__)

# 音频后缀到 MIME 类型的映射，Files API 需要显式声明。
_MIME_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


class ASRServiceError(RuntimeError):
    """语音识别上游调用失败。"""


class ASRService:
    """语音转文字服务。

    provider=gemini 走 Files API 上传 + /v1beta/interactions；
    其他厂商走 OpenAI 兼容的 /audio/transcriptions。
    """

    def __init__(self):
        self.provider = settings.ASR_PROVIDER
        self.model = settings.ASR_MODEL
        self.base_url = settings.ASR_BASE_URL.rstrip("/")
        self.api_key = settings.ASR_API_KEY
        self.client = AsyncOpenAI(
            api_key=self.api_key or "not-configured",
            base_url=self.base_url,
            http_client=build_async_client(),
        )

    async def transcribe(self, audio_data: bytes, filename: str = "audio.wav") -> str:
        """
        将音频转为文字
        :param audio_data: 音频字节数据
        :param filename: 文件名
        :return: 转写文本
        """
        if self.provider == "gemini":
            return await self._transcribe_gemini(audio_data, filename)
        return await self._transcribe_openai(audio_data, filename)

    async def _transcribe_openai(self, audio_data: bytes, filename: str) -> str:
        """调用 OpenAI 兼容的 /audio/transcriptions（Groq、OpenAI、硅基流动都适用）。"""
        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename

        params: dict = {"model": self.model, "file": audio_file}
        if settings.ASR_LANGUAGE:
            params["language"] = settings.ASR_LANGUAGE

        try:
            response = await self.client.audio.transcriptions.create(
                response_format="text",
                **params,
            )
        except Exception as error:  # noqa: BLE001
            raise ASRServiceError(f"语音识别请求失败: {error}") from error

        return str(response).strip()

    async def _transcribe_gemini(self, audio_data: bytes, filename: str) -> str:
        """先上传到 Files API，再交给 gemini-3.5-transcribe 转写。

        这个模型只在 /v1beta/interactions 上提供，且只接受文件 URI，
        所以是「上传 + 建 interaction」两步。
        """
        mime_type = self._guess_mime_type(filename)
        upload_base = self.base_url.replace("/v1beta", "/upload/v1beta")
        headers = {"x-goog-api-key": self.api_key}
        file_name: str | None = None

        try:
            async with build_async_client(300) as client:
                start = await client.post(
                    f"{upload_base}/files",
                    headers={
                        **headers,
                        "X-Goog-Upload-Protocol": "resumable",
                        "X-Goog-Upload-Command": "start",
                        "X-Goog-Upload-Header-Content-Length": str(len(audio_data)),
                        "X-Goog-Upload-Header-Content-Type": mime_type,
                        "Content-Type": "application/json",
                    },
                    json={"file": {"display_name": filename}},
                )
                upload_url = start.headers.get("x-goog-upload-url")
                if not upload_url:
                    raise ASRServiceError(
                        f"创建上传会话失败 {start.status_code}: {start.text[:200]}"
                    )

                uploaded = await client.post(
                    upload_url,
                    headers={
                        "Content-Length": str(len(audio_data)),
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                    content=audio_data,
                )
                if uploaded.status_code >= 400:
                    raise ASRServiceError(
                        f"上传音频失败 {uploaded.status_code}: {uploaded.text[:200]}"
                    )
                file_info = (uploaded.json() or {}).get("file") or {}
                file_uri = file_info.get("uri")
                file_name = file_info.get("name")
                if not file_uri:
                    raise ASRServiceError("上传音频失败：响应里没有文件 URI")

                response = await client.post(
                    f"{self.base_url}/interactions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "input": [
                            {
                                "type": "audio",
                                "uri": file_uri,
                                "mime_type": mime_type,
                            }
                        ],
                        "generation_config": {
                            "transcription_config": {
                                "language_codes": (
                                    [settings.ASR_LANGUAGE]
                                    if settings.ASR_LANGUAGE
                                    else []
                                ),
                            }
                        },
                    },
                )
                if response.status_code >= 400:
                    raise ASRServiceError(
                        f"语音识别接口返回 {response.status_code}: {response.text[:200]}"
                    )
                return self._extract_interaction_text(response.json())
        except httpx.HTTPError as error:
            raise ASRServiceError(f"语音识别请求失败: {error}") from error
        finally:
            if file_name:
                await self._delete_file(file_name)

    async def _delete_file(self, file_name: str) -> None:
        """删掉 Files API 上的临时文件，失败不影响转写结果。"""
        try:
            async with build_async_client(30) as client:
                await client.delete(
                    f"{self.base_url}/{file_name}",
                    headers={"x-goog-api-key": self.api_key},
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("清理转写临时文件失败: %s", error)

    @staticmethod
    def _extract_interaction_text(body: dict) -> str:
        """从 interactions 响应里取文本，兼容 output_text 和 steps 两种形态。"""
        if not isinstance(body, dict):
            raise ASRServiceError("语音识别返回了非预期格式")

        text = body.get("output_text")
        if isinstance(text, str) and text.strip():
            return text.strip()

        parts: list[str] = []
        for step in body.get("steps") or []:
            for content in (step or {}).get("content") or []:
                if not isinstance(content, dict):
                    continue
                chunk = content.get("text")
                if isinstance(chunk, str):
                    parts.append(chunk)
        if parts:
            return "".join(parts).strip()

        error = (body.get("error") or {}).get("message") if body.get("error") else None
        raise ASRServiceError(f"语音识别失败: {error or body}")

    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        lowered = filename.lower()
        for suffix, mime in _MIME_TYPES.items():
            if lowered.endswith(suffix):
                return mime
        return "audio/wav"

    async def transcribe_with_summary(self, audio_data: bytes, filename: str = "audio.wav") -> dict:
        """
        语音转文字并生成摘要
        :return: {"text": "原始文本", "summary": "摘要"}
        """
        text = await self.transcribe(audio_data, filename)

        # 生成简短摘要（取前100字）
        summary = text[:100] + "..." if len(text) > 100 else text

        return {
            "text": text,
            "summary": summary,
        }
