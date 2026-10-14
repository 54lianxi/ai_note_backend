import pytest
from fastapi import HTTPException
from minio.error import S3Error

from app.api import media


def _s3_error(code: str = "NoSuchKey") -> S3Error:
    return S3Error(code, "not found", "resource", "request-id", "host-id", None)


class _FakeStat:
    def __init__(self, size: int, content_type: str):
        self.size = size
        self.content_type = content_type


class _FakeResponse:
    def __init__(self, data: bytes):
        self.data = data
        self.closed = False

    def stream(self, amt: int):
        yield self.data

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


class _FakeInnerClient:
    def __init__(self, data: bytes, size: int, content_type: str, missing: bool):
        self.data = data
        self.size = size
        self.content_type = content_type
        self.missing = missing
        self.range_args: tuple[int | None, int | None] | None = None
        self.response: _FakeResponse | None = None

    def stat_object(self, bucket: str, object_name: str):
        if self.missing:
            raise _s3_error()
        return _FakeStat(self.size, self.content_type)

    def get_object(self, bucket: str, object_name: str, offset=None, length=None):
        self.range_args = (offset, length)
        self.response = _FakeResponse(self.data)
        return self.response


class _FakeMinIO:
    def __init__(self, data=b"abcdefghij", size=10, content_type="audio/wav", missing=False):
        self.bucket = "records"
        self.client = _FakeInnerClient(data, size, content_type, missing)


def _use_fake(monkeypatch, fake: _FakeMinIO) -> None:
    monkeypatch.setattr(media, "get_minio_client", lambda: fake)


async def _body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("records/records/voice_1.wav", "records/voice_1.wav"),
        ("records/voice_1.wav", "records/voice_1.wav"),
        ("voice_1.wav", "voice_1.wav"),
        ("", ""),
    ],
)
def test_resolve_object_name_strips_bucket_prefix(path, expected):
    assert media.resolve_object_name(path) == expected


@pytest.mark.parametrize(
    ("raw", "size", "expected"),
    [
        (None, 10, None),
        ("bytes=0-3", 10, (0, 3)),
        ("bytes=4-", 10, (4, 9)),
        ("bytes=-3", 10, (7, 9)),
        ("bytes=0-100", 10, (0, 9)),
        ("bytes=10-", 10, None),
        ("bytes=5-2", 10, None),
        ("bytes=abc", 10, None),
        ("items=0-3", 10, None),
        ("bytes=0-3", 0, None),
    ],
)
def test_parse_range(raw, size, expected):
    assert media._parse_range(raw, size) == expected


@pytest.mark.asyncio
async def test_media_returns_full_object(monkeypatch):
    fake = _FakeMinIO()
    _use_fake(monkeypatch, fake)

    response = await media.get_media("records/records/voice_1.wav", None)

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10"
    assert await _body(response) == b"abcdefghij"
    assert fake.client.response.closed is True


@pytest.mark.asyncio
async def test_media_serves_range_requests(monkeypatch):
    fake = _FakeMinIO()
    _use_fake(monkeypatch, fake)

    response = await media.get_media("records/voice_1.wav", "bytes=2-4")

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-4/10"
    assert response.headers["content-length"] == "3"
    assert fake.client.range_args == (2, 3)


@pytest.mark.asyncio
async def test_media_returns_404_for_missing_object(monkeypatch):
    _use_fake(monkeypatch, _FakeMinIO(missing=True))

    with pytest.raises(HTTPException) as error:
        await media.get_media("records/voice_1.wav", None)

    assert error.value.status_code == 404
