"""实时转写上游会话重建的测试：模拟 Gemini Live 连接到期被断开。"""
import asyncio
import base64
import json

import pytest

from app.services import realtime_asr_service
from app.services.realtime_asr_service import RealtimeASRService

class FakeUpstream:
    """模拟 Gemini Live 上游连接。

    silent_after_audio=N：收到第 N 条音频消息后静默断开（会话到期）。
    error_after_audio=N：收到第 N 条音频消息后抛 error（0 表示一建立就抛）。
    error_after_finalize：收到 audioStreamEnd 后抛 error（收尾阶段故障）。
    无任何触发时保持连接，直到被服务端的超时逻辑结束。
    """

    def __init__(
        self,
        *,
        silent_after_audio: int | None = None,
        error_after_audio: int | None = None,
        error: Exception = RuntimeError("upstream boom"),
        error_after_finalize: bool = False,
    ):
        self._silent_after_audio = silent_after_audio
        self._error_after_audio = error_after_audio
        self._error = error
        self._error_after_finalize = error_after_finalize
        self._audio_count = 0
        self._silent = False
        self._pending_error: Exception | None = None
        self.sent: list[str] = []
        self.closed = False

    async def __aenter__(self):
        if self._error_after_audio == 0:
            self._pending_error = self._error
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, message: str) -> None:
        self.sent.append(message)
        realtime = json.loads(message).get("realtimeInput", {})
        if realtime.get("audioStreamEnd"):
            if self._error_after_finalize:
                self._pending_error = self._error
            return
        if realtime.get("audio"):
            self._audio_count += 1
            if self._audio_count == self._silent_after_audio:
                self._silent = True
            if self._audio_count == self._error_after_audio:
                self._pending_error = self._error

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self._pending_error is not None:
                raise self._pending_error
            if self._silent:
                raise StopAsyncIteration
            await asyncio.sleep(0.01)


def _service() -> RealtimeASRService:
    service = RealtimeASRService.__new__(RealtimeASRService)
    service.provider = "gemini"
    service.model = "gemini-test"
    service.api_key = "test-key"
    return service


def _frames(frames: list[bytes]):
    """一次性产完：用于收尾阶段等不需要长流式场景。"""
    async def generator():
        for frame in frames:
            yield frame

    return generator()


def _streaming_frames(frames: list[bytes], gap: float = 0.05):
    """像真实麦克风一样按时间间隔产出，让上游会话有“进行中”的窗口。"""
    async def generator():
        for frame in frames:
            yield frame
            await asyncio.sleep(gap)

    return generator()


def _audio_messages(upstream: FakeUpstream) -> list[bytes]:
    decoded = []
    for message in upstream.sent:
        payload = json.loads(message)
        data = (
            payload.get("realtimeInput", {})
            .get("audio", {})
            .get("data")
        )
        if data:
            decoded.append(base64.b64decode(data))
    return decoded


@pytest.fixture
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(realtime_asr_service, "_FINALIZE_TIMEOUT", 0.2)
    monkeypatch.setattr(realtime_asr_service, "_IDLE_TIMEOUT", 0.2)
    monkeypatch.setattr(realtime_asr_service, "_RECONNECT_BASE_SECONDS", 0.01)


@pytest.mark.asyncio
async def test_silent_close_midstream_reconnects_and_replays(
    monkeypatch, fast_timeouts
):
    service = _service()
    connections: list[FakeUpstream] = []

    def fake_connect(*args, **kwargs):
        if not connections:
            upstream = FakeUpstream(silent_after_audio=2)
        else:
            upstream = FakeUpstream()
        connections.append(upstream)
        return upstream

    # websockets>=14 的 connect() 返回 async context manager 对象而非协程。
    monkeypatch.setattr(realtime_asr_service.websockets, "connect", fake_connect)

    frames = [b"f1", b"f2", b"f3", b"f4"]
    events = [
        event async for event in service.transcribe(_streaming_frames(frames))
    ]

    # 第一次会话静默断开后自动重建了第二个会话。
    assert len(connections) == 2
    assert connections[0].closed
    # 新会话把全部音频从头重放，顺序不变，最后正常收尾。
    assert _audio_messages(connections[1]) == frames
    assert json.loads(connections[1].sent[-1]) == {
        "realtimeInput": {"audioStreamEnd": True}
    }
    # 对外只看到 transcript 和最后的 done，断线过程对前端透明。
    assert [e["type"] for e in events if e.get("type") != "transcript"] == [
        "done"
    ]


@pytest.mark.asyncio
async def test_upstream_error_midstream_reconnects(monkeypatch, fast_timeouts):
    service = _service()
    connections: list[FakeUpstream] = []

    def fake_connect(*args, **kwargs):
        if not connections:
            upstream = FakeUpstream(
                error_after_audio=2, error=RuntimeError("upstream boom")
            )
        else:
            upstream = FakeUpstream()
        connections.append(upstream)
        return upstream

    monkeypatch.setattr(realtime_asr_service.websockets, "connect", fake_connect)

    frames = [b"f1", b"f2", b"f3"]
    events = [
        event async for event in service.transcribe(_streaming_frames(frames))
    ]

    assert len(connections) == 2
    assert _audio_messages(connections[1]) == frames
    assert [e["type"] for e in events if e.get("type") != "transcript"] == [
        "done"
    ]


@pytest.mark.asyncio
async def test_repeated_failures_give_up(monkeypatch, fast_timeouts):
    service = _service()

    def fake_connect(*args, **kwargs):
        return FakeUpstream(error_after_audio=0)

    monkeypatch.setattr(realtime_asr_service.websockets, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="已放弃重连"):
        # 音频流要足够长：指数退避总计约 0.31 秒，帧窗口必须明显更长，
        # 否则音频先发完（eof 置位）就变成收尾阶段的正常抛错。
        async for _ in service.transcribe(
            _streaming_frames([b"f1"] * 20, gap=0.05)
        ):
            pass


@pytest.mark.asyncio
async def test_error_during_finalize_is_surfaced(monkeypatch, fast_timeouts):
    """收尾阶段（音频已发完）的上游错误按原样抛出，不触发重建。"""
    service = _service()

    def fake_connect(*args, **kwargs):
        return FakeUpstream(
            error_after_finalize=True,
            error=RuntimeError("boom after finalize"),
        )

    monkeypatch.setattr(realtime_asr_service.websockets, "connect", fake_connect)

    with pytest.raises(RuntimeError, match="boom after finalize"):
        async for _ in service.transcribe(_frames([b"f1"])):
            pass
