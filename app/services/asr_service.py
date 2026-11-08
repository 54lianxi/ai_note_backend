"""ASR 语音识别服务"""
import asyncio
import logging
import os
import shutil
import tempfile
import wave

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core.http import build_async_client
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

# 分块上传的块大小和单次请求超时。长录音转写可能跑十几分钟，留够时间。
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_TRANSCRIBE_TIMEOUT = 1800.0

# gemini-3.5-transcribe 单次请求最多接受 1 小时音频（未开说话人分离/时间戳时）。
# 超过会被上游 4xx 拒绝，所以把长 WAV 按 55 分钟切段分别转写再拼接，
# 留 5 分钟余量防元数据偏差。非 WAV 格式不做分段，保持原行为。
_SEGMENT_SECONDS = 55 * 60
_SEGMENT_READ_BYTES = 1024 * 1024

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

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


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

    async def transcribe_file(
        self,
        path: str,
        filename: str = "audio.wav",
    ) -> str:
        """
        将磁盘上的音频文件转为文字。
        :param path: 本地临时文件路径
        :param filename: 原始文件名（用于判断 MIME 和展示）
        :return: 转写文本
        """
        if self.provider == "gemini":
            segments = await self._split_long_wav(path)
            if segments:
                try:
                    texts = []
                    for segment in segments:
                        text = await self._transcribe_with_retry(segment, filename)
                        if text:
                            texts.append(text)
                    return "\n".join(texts)
                finally:
                    await asyncio.to_thread(
                        shutil.rmtree, os.path.dirname(segments[0]), True
                    )
        return await self._transcribe_with_retry(path, filename)

    async def _transcribe_with_retry(self, path: str, filename: str) -> str:
        """单条音频的转写（带指数退避重试）。长录音分段后逐段调用这里。"""
        attempts = settings.ASR_MAX_RETRIES + 1
        for attempt in range(attempts):
            try:
                if self.provider == "gemini":
                    return await self._transcribe_gemini_file(path, filename)
                return await self._transcribe_openai_file(path, filename)
            except ASRServiceError as error:
                if not error.retryable or attempt + 1 >= attempts:
                    raise
                delay = settings.ASR_RETRY_BASE_SECONDS * (2**attempt)
                logger.warning(
                    "ASR 临时失败，%.1f 秒后重试 (%s/%s): %s",
                    delay,
                    attempt + 1,
                    settings.ASR_MAX_RETRIES,
                    error,
                )
                await asyncio.sleep(delay)

        raise ASRServiceError("语音识别失败")

    async def _split_long_wav(self, path: str) -> list[str]:
        """把超过单次转写时长上限的 WAV 按 PCM 帧切成段。

        返回分段文件路径列表；时长未超限、不是 WAV 或文件读不了时返回 []，
        调用方回落到整文件转写。WAV 是裸 PCM，按帧对齐的字节切块即无损。
        """
        try:
            info = await asyncio.to_thread(self._probe_wav, path)
        except Exception:  # noqa: BLE001  # 非 WAV 或文件不可读，不分段
            return []
        if info is None:
            return []
        framerate, total_frames = info
        duration = total_frames / framerate
        if duration <= _SEGMENT_SECONDS:
            return []
        return await asyncio.to_thread(self._write_wav_segments, path, info)

    @staticmethod
    def _probe_wav(path: str) -> tuple[int, int] | None:
        """读 WAV 参数，返回 (帧率, 总帧数)；非 WAV 返回 None。"""
        with wave.open(path, "rb") as source:
            framerate = source.getframerate()
            total_frames = source.getnframes()
        if framerate <= 0 or total_frames <= 0:
            return None
        return framerate, total_frames

    @staticmethod
    def _write_wav_segments(path: str, info: tuple[int, int]) -> list[str]:
        """按帧对齐把 WAV 切成多个不超过单段时长上限的片段文件。"""
        framerate, total_frames = info
        with wave.open(path, "rb") as source:
            params = source.getparams()
            frames_per_segment = framerate * _SEGMENT_SECONDS
            # 一次读 1MB 的帧，避免把整段读进内存
            block_frames = max(
                1, _SEGMENT_READ_BYTES // (params.sampwidth * params.nchannels)
            )
            segment_dir = tempfile.mkdtemp(prefix="asr-split-")
            segments: list[str] = []
            try:
                remaining = total_frames
                while remaining > 0:
                    frames_this_segment = min(frames_per_segment, remaining)
                    remaining -= frames_this_segment
                    segment_path = f"{segment_dir}/segment-{len(segments):03d}.wav"
                    segments.append(segment_path)
                    with wave.open(segment_path, "wb") as writer:
                        writer.setparams(params)
                        frames_left = frames_this_segment
                        while frames_left > 0:
                            block = source.readframes(min(frames_left, block_frames))
                            if not block:
                                break
                            writer.writeframes(block)
                            frames_left -= (
                                len(block) // (params.sampwidth * params.nchannels)
                            )
            except Exception:
                shutil.rmtree(segment_dir, True)
                raise
        return segments

    async def _transcribe_openai_file(self, path: str, filename: str) -> str:
        """调用 OpenAI 兼容的 /audio/transcriptions（Groq、OpenAI、硅基流动都适用）。"""
        params: dict = {"model": self.model}
        if settings.ASR_LANGUAGE:
            params["language"] = settings.ASR_LANGUAGE

        try:
            with open(path, "rb") as audio_file:
                response = await self.client.audio.transcriptions.create(
                    response_format="text",
                    file=(filename, audio_file),
                    **params,
                )
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            retryable = (
                status_code == 429
                or (isinstance(status_code, int) and status_code >= 500)
                or error.__class__.__name__
                in {"APIConnectionError", "APITimeoutError"}
            )
            raise ASRServiceError(
                f"语音识别请求失败: {error}",
                status_code=status_code,
                retryable=retryable,
            ) from error

        return str(response).strip()

    async def _upload_gemini_file(
        self,
        client: httpx.AsyncClient,
        upload_base: str,
        headers: dict[str, str],
        path: str,
        filename: str,
        mime_type: str,
        size: int,
    ) -> dict:
        """按块流式上传到 Files API，长录音不经过内存。"""
        start = await client.post(
            f"{upload_base}/files",
            headers={
                **headers,
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": filename}},
        )
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise ASRServiceError(
                f"创建上传会话失败 {start.status_code}: {start.text[:200]}",
                status_code=start.status_code,
                retryable=self._retryable_status(start.status_code),
            )

        offset = 0
        uploaded = start
        with open(path, "rb") as source:
            while True:
                chunk = await asyncio.to_thread(source.read, _UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                offset += len(chunk)
                final = offset >= size
                uploaded = await client.post(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "X-Goog-Upload-Offset": str(offset - len(chunk)),
                        "X-Goog-Upload-Command": (
                            "upload, finalize" if final else "upload"
                        ),
                    },
                    content=chunk,
                )
                if uploaded.status_code >= 400:
                    raise ASRServiceError(
                        f"上传音频失败 {uploaded.status_code}: {uploaded.text[:200]}",
                        status_code=uploaded.status_code,
                        retryable=self._retryable_status(uploaded.status_code),
                    )

        return (uploaded.json() or {}).get("file") or {}

    async def _transcribe_gemini_file(self, path: str, filename: str) -> str:
        """先上传到 Files API，再交给 gemini-3.5-transcribe 转写。

        这个模型只在 /v1beta/interactions 上提供，且只接受文件 URI，
        所以是「上传 + 建 interaction」两步。上传按 8MB 分块，内存占用恒定。
        """
        mime_type = self._guess_mime_type(filename)
        upload_base = self.base_url.replace("/v1beta", "/upload/v1beta")
        headers = {"x-goog-api-key": self.api_key}
        size = os.path.getsize(path)
        file_name: str | None = None
        if size == 0:
            raise ASRServiceError("音频为空")

        try:
            async with build_async_client(_TRANSCRIBE_TIMEOUT) as client:
                file_info = await self._upload_gemini_file(
                    client,
                    upload_base,
                    headers,
                    path,
                    filename,
                    mime_type,
                    size,
                )
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
                        f"语音识别接口返回 {response.status_code}: {response.text[:200]}",
                        status_code=response.status_code,
                        retryable=self._retryable_status(response.status_code),
                    )
                return self._extract_interaction_text(response.json())
        except httpx.HTTPError as error:
            raise ASRServiceError(
                f"语音识别请求失败: {error}",
                retryable=True,
            ) from error
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

    @staticmethod
    def _retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    async def transcribe_with_summary_file(
        self,
        path: str,
        filename: str = "audio.wav",
        language: str | None = None,
    ) -> dict:
        """
        语音转文字并生成摘要
        :return: {"text": "原始文本", "summary": "摘要"}
        """
        text = await self.transcribe_file(path, filename)
        summary = ""
        if text:
            try:
                summary = await LLMService().summarize_record(text, language)
            except Exception:
                # 摘要失败不能影响录音和转写保存；长文本保留开头供回看。
                logger.warning("语音摘要生成失败", exc_info=True)
                if len(text) > 100:
                    summary = f"{text[:100]}..."

        return {
            "text": text,
            "summary": summary,
        }
