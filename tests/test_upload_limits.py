import pytest
from fastapi import HTTPException

from app.api.record import _read_upload


class _FakeUpload:
    def __init__(self, data: bytes, size: int | None):
        self.data = data
        self.size = size
        self.was_read = False
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        self.was_read = True
        if size < 0:
            chunk = self.data[self.offset :]
        else:
            chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_read_upload_rejects_known_oversized_file_before_reading():
    file = _FakeUpload(data=b"x", size=1024 * 1024 + 1)

    with pytest.raises(HTTPException) as error:
        await _read_upload(file, max_mb=1, language="zh")

    assert error.value.status_code == 413
    assert "1 MB" in error.value.detail
    assert file.was_read is False


@pytest.mark.asyncio
async def test_read_upload_rejects_oversized_stream_without_reading_it_all():
    file = _FakeUpload(data=b"x" * (1024 * 1024 + 1), size=None)

    with pytest.raises(HTTPException) as error:
        await _read_upload(file, max_mb=1, language="en")

    assert error.value.status_code == 413
    assert "1 MB" in error.value.detail


@pytest.mark.asyncio
async def test_read_upload_returns_file_at_limit():
    data = b"x" * (1024 * 1024)

    assert await _read_upload(_FakeUpload(data, size=None), 1, "zh") == data
