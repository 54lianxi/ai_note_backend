"""长音频分段转写的单元测试。"""

import os
import struct
import wave

import pytest

from app.services import asr_service
from app.services.asr_service import ASRService


def _service() -> ASRService:
    service = ASRService.__new__(ASRService)
    service.provider = "gemini"
    return service


def _write_wav(path: str, framerate: int, nframes: int) -> None:
    with wave.open(path, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(framerate)
        target.writeframes(b"".join(
            struct.pack("<h", 100) for _ in range(nframes)
        ))


@pytest.mark.asyncio
async def test_long_wav_is_split_and_text_joined(monkeypatch, tmp_path):
    """超过单段时长的 WAV 切成多段转写，文本按顺序拼接，临时段文件清理。"""
    monkeypatch.setattr(asr_service, "_SEGMENT_SECONDS", 1)
    # 2.5 秒的音频 → 按每段 1 秒切成 3 段
    path = str(tmp_path / "long.wav")
    _write_wav(path, framerate=2, nframes=5)

    service = _service()
    calls: list[str] = []

    async def fake_transcribe(segment_path, _filename):
        calls.append(segment_path)
        return f"text-{len(calls)}"

    monkeypatch.setattr(service, "_transcribe_gemini_file", fake_transcribe)

    result = await service.transcribe_file(path, "long.wav")

    assert len(calls) == 3
    assert result == "text-1\ntext-2\ntext-3"
    # 段文件和临时目录都要清掉
    assert not any(os.path.exists(p) for p in calls)


@pytest.mark.asyncio
async def test_short_wav_is_not_split(monkeypatch, tmp_path):
    """未超时长的音频不切段，直接转写原文件。"""
    monkeypatch.setattr(asr_service, "_SEGMENT_SECONDS", 60)
    path = str(tmp_path / "short.wav")
    _write_wav(path, framerate=2, nframes=5)

    service = _service()
    calls: list[str] = []

    async def fake_transcribe(segment_path, _filename):
        calls.append(segment_path)
        return "ok"

    monkeypatch.setattr(service, "_transcribe_gemini_file", fake_transcribe)

    assert await service.transcribe_file(path, "short.wav") == "ok"
    assert calls == [path]


@pytest.mark.asyncio
async def test_non_wav_falls_back_to_single_transcription(monkeypatch, tmp_path):
    """非 WAV 文件无法按帧切段，回落整文件转写。"""
    path = tmp_path / "audio.m4a"
    path.write_bytes(b"\x00\x00\x00\x18ftypM4A fake")

    service = _service()
    calls: list[str] = []

    async def fake_transcribe(segment_path, _filename):
        calls.append(segment_path)
        return "ok"

    monkeypatch.setattr(service, "_transcribe_gemini_file", fake_transcribe)

    assert await service.transcribe_file(str(path), "audio.m4a") == "ok"
    assert calls == [str(path)]
