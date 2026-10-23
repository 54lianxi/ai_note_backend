import pytest

from app.services import asr_service
from app.services.asr_service import ASRService, ASRServiceError


def _service() -> ASRService:
    service = ASRService.__new__(ASRService)
    service.provider = "gemini"
    return service


@pytest.mark.asyncio
async def test_transient_asr_error_retries_with_backoff(monkeypatch):
    service = _service()
    attempts = 0
    delays: list[float] = []

    async def fake_transcribe(_data, _filename):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ASRServiceError(
                "high demand",
                status_code=503,
                retryable=True,
            )
        return "转写成功"

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(service, "_transcribe_gemini", fake_transcribe)
    monkeypatch.setattr(asr_service.asyncio, "sleep", fake_sleep)

    result = await service.transcribe(b"audio", "audio.wav")

    assert result == "转写成功"
    assert attempts == 3
    assert delays == [2.0, 4.0]


@pytest.mark.asyncio
async def test_non_retryable_asr_error_fails_immediately(monkeypatch):
    service = _service()
    attempts = 0
    delays: list[float] = []

    async def fake_transcribe(_data, _filename):
        nonlocal attempts
        attempts += 1
        raise ASRServiceError(
            "invalid api key",
            status_code=401,
            retryable=False,
        )

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(service, "_transcribe_gemini", fake_transcribe)
    monkeypatch.setattr(asr_service.asyncio, "sleep", fake_sleep)

    with pytest.raises(ASRServiceError, match="invalid api key"):
        await service.transcribe(b"audio", "audio.wav")

    assert attempts == 1
    assert delays == []
