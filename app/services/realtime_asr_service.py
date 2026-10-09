"""实时语音转写代理。"""
import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from app.config import settings

logger = logging.getLogger(__name__)

# 客户端上送的 PCM 采样率，Gemini Live API 要求写在 MIME 里。
_SAMPLE_RATE = 16000
# 音频发完后，还没拿到任何终稿时最多等多久。
_FINALIZE_TIMEOUT = 15.0
# 已经拿到终稿后，再多等一会儿看有没有后续片段。
_IDLE_TIMEOUT = 2.0


class RealtimeASRService:
    """把客户端 PCM 音频帧代理到上游实时 ASR。

    provider=gemini 走 Live API 的 BidiGenerateContent；
    provider=openai 走 OpenAI Realtime 协议。
    对前端统一输出 transcript / error / done 三种事件。
    """

    def __init__(self) -> None:
        self.provider = settings.ASR_REALTIME_PROVIDER
        self.model = settings.ASR_REALTIME_MODEL
        self.api_key = settings.ASR_API_KEY

    async def transcribe(
        self,
        audio_frames: AsyncIterator[bytes],
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("实时转写未配置 API Key")

        if self.provider == "gemini":
            async for event in self._transcribe_gemini(audio_frames):
                yield event
        else:
            async for event in self._transcribe_openai(audio_frames):
                yield event

    # ------------------------------------------------------------------
    # Gemini Live API
    # ------------------------------------------------------------------
    async def _transcribe_gemini(
        self,
        audio_frames: AsyncIterator[bytes],
    ) -> AsyncIterator[dict[str, Any]]:
        separator = "&" if "?" in settings.ASR_REALTIME_URL else "?"
        upstream_url = f"{settings.ASR_REALTIME_URL}{separator}key={self.api_key}"
        segments: list[str] = []

        async with websockets.connect(
            upstream_url,
            max_size=None,
            ping_interval=20,
            proxy=settings.ASR_REALTIME_PROXY or None,
        ) as upstream:
            await upstream.send(json.dumps({
                "setup": {
                    "model": self._qualified_model(),
                    "generationConfig": {"responseModalities": ["TEXT"]},
                    "inputAudioTranscription": {
                        "languageCodes": (
                            [settings.ASR_REALTIME_LANGUAGE]
                            if settings.ASR_REALTIME_LANGUAGE
                            else []
                        ),
                    },
                },
            }))

            events: asyncio.Queue[dict[str, Any] | None | Exception] = asyncio.Queue()
            sender_task = asyncio.create_task(
                self._send_audio_gemini(upstream, audio_frames)
            )
            reader_task = asyncio.create_task(self._read_events_gemini(upstream, events))
            got_final = False
            try:
                while True:
                    event = await self._next_event(
                        events,
                        sender_task,
                        _IDLE_TIMEOUT if got_final else _FINALIZE_TIMEOUT,
                    )
                    if event is None:
                        break
                    if isinstance(event, Exception):
                        raise event
                    if event.get("type") == "done":
                        yield event
                        break
                    if event.get("type") == "turn_complete":
                        # 音频还没发完就收到 turnComplete，说明只是一句话结束，
                        # 后面还会继续；等发送端收尾了再收工。
                        if sender_task.done():
                            yield {"type": "done"}
                            break
                        continue
                    event["text"] = self._commit_segment(segments, event)
                    if event.get("final"):
                        got_final = True
                    yield event
            finally:
                for task in (sender_task, reader_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender_task, reader_task, return_exceptions=True)

    async def _send_audio_gemini(self, upstream, audio_frames) -> None:
        async for frame in audio_frames:
            if not frame:
                continue
            await upstream.send(json.dumps({
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(frame).decode("ascii"),
                        "mimeType": f"audio/pcm;rate={_SAMPLE_RATE}",
                    }
                }
            }))
        # 音频发完立刻告诉服务端收尾，能显著降低最后一段终稿的延迟。
        await upstream.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    async def _read_events_gemini(
        self,
        upstream,
        events: "asyncio.Queue[dict[str, Any] | None | Exception]",
    ) -> None:
        try:
            async for raw in upstream:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if message.get("error"):
                    error = message["error"]
                    raise RuntimeError(error.get("message") or "实时语音转写失败")

                content = message.get("serverContent")
                if not content:
                    continue

                interim = (content.get("interimInputTranscription") or {}).get("text")
                if interim:
                    await events.put({
                        "type": "transcript",
                        "text": interim,
                        "final": False,
                    })

                final = (content.get("inputTranscription") or {}).get("text")
                if final:
                    await events.put({
                        "type": "transcript",
                        "text": final,
                        "final": True,
                    })

                if content.get("turnComplete"):
                    await events.put({"type": "turn_complete"})
        except Exception as error:  # noqa: BLE001
            await events.put(error)
        finally:
            await events.put(None)

    async def _next_event(
        self,
        events: "asyncio.Queue[dict[str, Any] | None | Exception]",
        sender_task: asyncio.Task,
        timeout: float,
    ) -> dict[str, Any] | None | Exception:
        """取下一个上游事件。

        音频已经发完时，静默超过 timeout 就当结束，避免录音卡住不出结果。
        """
        if not sender_task.done():
            return await events.get()

        try:
            return await asyncio.wait_for(events.get(), timeout=timeout)
        except TimeoutError:
            return {"type": "done"}

    # ------------------------------------------------------------------
    # OpenAI Realtime 协议
    # ------------------------------------------------------------------
    async def _transcribe_openai(
        self,
        audio_frames: AsyncIterator[bytes],
    ) -> AsyncIterator[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        separator = "&" if "?" in settings.ASR_REALTIME_URL else "?"
        upstream_url = (
            f"{settings.ASR_REALTIME_URL}{separator}"
            f"model={self.model}"
        )
        async with websockets.connect(
            upstream_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=20,
            proxy=settings.ASR_REALTIME_PROXY or None,
        ) as upstream:
            await upstream.send(json.dumps({
                "event_id": "session_update",
                "type": "session.update",
                "session": {
                    "modalities": ["text"],
                    "input_audio_format": "pcm",
                    "sample_rate": _SAMPLE_RATE,
                    "input_audio_transcription": {
                        "language": settings.ASR_REALTIME_LANGUAGE or "zh",
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.2,
                        "silence_duration_ms": 400,
                    },
                },
            }))

            events: asyncio.Queue[dict[str, Any] | None | Exception] = asyncio.Queue()

            async def receive_events() -> None:
                try:
                    async for event in self._receive_events(upstream):
                        await events.put(event)
                except Exception as error:  # noqa: BLE001
                    await events.put(error)
                finally:
                    await events.put(None)

            receive_task = asyncio.create_task(receive_events())
            send_task = asyncio.create_task(self._send_audio(upstream, audio_frames))
            try:
                while True:
                    event = await events.get()
                    if isinstance(event, Exception):
                        raise event
                    if event is None:
                        break
                    yield event
                    if event.get("type") == "done":
                        break
            finally:
                for task in (receive_task, send_task):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (receive_task, send_task) if task is not None),
                    return_exceptions=True,
                )

    async def _send_audio(
        self,
        upstream,
        audio_frames: AsyncIterator[bytes],
    ) -> None:
        event_index = 0
        async for frame in audio_frames:
            if not frame:
                continue
            event_index += 1
            await upstream.send(json.dumps({
                "event_id": f"audio_{event_index}",
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }))
        await upstream.send(json.dumps({
            "event_id": "session_finish",
            "type": "session.finish",
        }))

    async def _receive_events(self, upstream) -> AsyncIterator[dict[str, Any]]:
        async for raw in upstream:
            if isinstance(raw, bytes):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            if event_type == "conversation.item.input_audio_transcription.text":
                text = event.get("delta") or event.get("text")
                if text:
                    yield {"type": "transcript", "text": str(text), "final": False}
            elif event_type == "conversation.item.input_audio_transcription.completed":
                text = (
                    event.get("transcript")
                    or event.get("text")
                    or event.get("delta")
                )
                if text:
                    yield {"type": "transcript", "text": str(text), "final": True}
            elif event_type == "session.finished":
                yield {"type": "done"}
                return
            elif event_type in {"error", "session.error"}:
                error = event.get("error", {})
                raise RuntimeError(error.get("message", "实时语音转写失败"))

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def _qualified_model(self) -> str:
        if self.provider == "gemini" and not self.model.startswith("models/"):
            return f"models/{self.model}"
        return self.model

    @staticmethod
    def _commit_segment(segments: list[str], event: dict[str, Any]) -> str:
        """把上游的转写片段拼成完整文本。

        Gemini 的终稿是按「一轮说话」给的，有时是这一轮的最新版本、
        有时是新增的一段，所以用前缀判断来兼容两种语义，避免重复。
        """
        segment = str(event.get("text", "")).strip()
        if not segment:
            return "\n".join(segments)

        if not event.get("final"):
            return "\n".join([*segments, segment])

        if segments and segment.startswith(segments[-1]):
            segments[-1] = segment
        elif segment not in segments:
            segments.append(segment)
        return "\n".join(segments)
