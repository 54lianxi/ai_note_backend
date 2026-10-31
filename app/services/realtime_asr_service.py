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
# 上游会话被强制结束时最多自动重建几次。
# Gemini Live 单连接约 10 分钟就会被服务端断开，长录音靠重建续命；
# 重建时只接着发还没发出去的音频，不会把整段录音重新识别一遍。
_MAX_SESSION_RECONNECTS = 30
_RECONNECT_BASE_SECONDS = 2.0


class RealtimeASRService:
    """把客户端 PCM 音频帧代理到上游实时 ASR。

    provider=gemini 走 Live API 的 BidiGenerateContent；
    provider=openai 走 OpenAI Realtime 协议。
    对前端统一输出 transcript / error / done 三种事件。

    上游会话有硬性时长上限，到期会被服务端静默断开；客户端网络抖动
    也会让前端重连。这里把已确认发出的音频位置和已完成的转写片段保留下来，
    上游会话非正常结束时只重建会话并接着发剩余音频，让长时间录音的转写
    不断档，也不会把已经显示的文字从头覆盖一遍。
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

        # 收到的所有音频帧。新会话只从 [next_index] 继续，不重放历史音频。
        buffered: list[bytes] = []
        # 下一帧要发给上游的位置：只有确认发完的帧才推进，断线时最多重发一帧。
        next_index = 0
        # 跨上游会话累积的终稿片段，重建后继续往后拼，不会从头再来。
        segments: list[str] = []
        # 客户端音频流结束（stop/cancel/掉线）后置位，会话正常收尾。
        eof = asyncio.Event()
        available = asyncio.Condition()

        async def pump() -> None:
            try:
                async for frame in audio_frames:
                    if not frame:
                        continue
                    async with available:
                        buffered.append(frame)
                        available.notify_all()
            finally:
                async with available:
                    eof.set()
                    available.notify_all()

        pump_task = asyncio.create_task(pump())

        def session_frames() -> AsyncIterator[bytes]:
            """一个上游会话的音频来源：从上次发到的位置继续，随后跟着新帧。

            [next_index] 在发送方取走下一帧时才推进，所以若会话在发送途中
            断开，最多只会把最后一帧再发一次，不会漏掉整段音频。
            """
            async def generator():
                nonlocal next_index
                index = next_index
                while True:
                    async with available:
                        while index >= len(buffered):
                            if eof.is_set():
                                return
                            await available.wait()
                    frame = buffered[index]
                    yield frame
                    # 发送方已经处理完这一帧（发出或退出）后才会走到这里。
                    next_index = index + 1
                    index += 1

            return generator()

        attempt = 0
        try:
            while True:
                ended_prematurely = False
                try:
                    async for event in self._run_provider(
                        session_frames(), segments
                    ):
                        yield event
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if eof.is_set():
                        # 音频已经发完（收尾阶段）才出错：按原样抛给前端。
                        raise
                    logger.warning("实时转写上游会话中断，准备重建: %s", error)
                    ended_prematurely = True

                if eof.is_set():
                    return

                if not ended_prematurely:
                    # 上游静默关闭（无异常），同样是会话到期的一种表现。
                    logger.info("实时转写上游会话到期，从断点继续")

                attempt += 1
                if attempt > _MAX_SESSION_RECONNECTS:
                    raise RuntimeError("实时转写上游会话多次中断，已放弃重连")
                backoff = min(_RECONNECT_BASE_SECONDS * (2 ** (attempt - 1)), 8.0)
                await asyncio.sleep(backoff)
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)

    async def _run_provider(
        self,
        frames: AsyncIterator[bytes],
        segments: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        if self.provider == "gemini":
            async for event in self._transcribe_gemini(frames, segments):
                yield event
        else:
            async for event in self._transcribe_openai(frames, segments):
                yield event

    # ------------------------------------------------------------------
    # Gemini Live API
    # ------------------------------------------------------------------
    async def _transcribe_gemini(
        self,
        audio_frames: AsyncIterator[bytes],
        segments: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        separator = "&" if "?" in settings.ASR_REALTIME_URL else "?"
        upstream_url = f"{settings.ASR_REALTIME_URL}{separator}key={self.api_key}"

        async with websockets.connect(
            upstream_url,
            max_size=None,
            ping_interval=20,
            # 上行是 base64 的小包，压缩只会加 CPU 和延迟。
            compression=None,
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

        发送端还在推音频时无限等；音频发完后给上游 timeout 秒吐最后的
        转写，超时就当结束。发送端若在等待中途收尾（或出错），立刻切换
        到限时等待，避免上游沉默时卡死。
        """
        queue_get = asyncio.create_task(events.get())
        try:
            if not sender_task.done():
                await asyncio.wait(
                    {queue_get, sender_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_get.done():
                    return queue_get.result()
            sender_error = sender_task.exception()
            if sender_error is not None:
                raise sender_error
            try:
                return await asyncio.wait_for(queue_get, timeout=timeout)
            except TimeoutError:
                return {"type": "done"}
        finally:
            if not queue_get.done():
                queue_get.cancel()
                await asyncio.gather(queue_get, return_exceptions=True)

    # ------------------------------------------------------------------
    # OpenAI Realtime 协议
    # ------------------------------------------------------------------
    async def _transcribe_openai(
        self,
        audio_frames: AsyncIterator[bytes],
        segments: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        # OpenAI 实时协议按增量片段下发，这里不需要跨会话拼接文本。
        del segments
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
                    event = await self._next_event(
                        events, send_task, _FINALIZE_TIMEOUT
                    )
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
