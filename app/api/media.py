"""媒体代理接口

对象存储（MinIO）只在服务器本机监听，客户端拿到的直连地址（localhost:9000）
在手机上根本连不上，所以图片和音频统一从 API 域名转发出去。音频播放器拖动
进度会发 Range 请求，这里一并支持。
"""
import logging
import mimetypes
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from minio.error import S3Error
from starlette.responses import StreamingResponse

from app.config import settings
from app.infrastructure.minio_client import get_minio_client

router = APIRouter(tags=["媒体"])
logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024


def resolve_object_name(path: str, bucket: str | None = None) -> str:
    """把 URL 路径还原成对象存储里的 key。

    历史 URL 形如 /records/records/voice_x.wav（bucket + key），也可能只带 key，
    这里只在确实带了 bucket 前缀时去掉它。
    """
    bucket = bucket or settings.MINIO_BUCKET
    prefix = f"{bucket}/"
    if path.startswith(prefix) and path.count("/") >= 2:
        return path[len(prefix):]
    return path


def _parse_range(raw: str | None, size: int) -> tuple[int, int] | None:
    """解析 Range 头，返回闭区间 (start, end)；没写或写坏了返回 None。"""
    if not raw or not raw.startswith("bytes=") or size <= 0:
        return None
    start_text, _, end_text = raw[len("bytes="):].split(",")[0].strip().partition("-")
    if not start_text:
        # bytes=-N：最后 N 个字节
        if not end_text.isdigit():
            return None
        length = min(int(end_text), size)
        if length <= 0:
            return None
        return size - length, size - 1
    if not start_text.isdigit():
        return None
    start = int(start_text)
    if start >= size:
        return None
    end = min(int(end_text), size - 1) if end_text.isdigit() else size - 1
    if end < start:
        return None
    return start, end


def _stream(response) -> Iterator[bytes]:
    """把 urllib3 响应体转成生成器，用完释放连接。"""
    try:
        yield from response.stream(_CHUNK_SIZE)
    finally:
        response.close()
        response.release_conn()


@router.get("/media/{path:path}")
async def get_media(
    path: str,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
):
    """读取图片/音频对象。公开访问，和原来对象存储的只读策略一致。"""
    object_name = resolve_object_name(path)
    if not object_name:
        raise HTTPException(status_code=404, detail="文件不存在")

    minio = get_minio_client()
    try:
        stat = minio.client.stat_object(minio.bucket, object_name)
    except S3Error as error:
        logger.info("媒体对象不存在: %s (%s)", object_name, error.code)
        raise HTTPException(status_code=404, detail="文件不存在") from error

    size = stat.size or 0
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=604800",
        "Content-Type": (
            stat.content_type
            or mimetypes.guess_type(object_name)[0]
            or "application/octet-stream"
        ),
    }

    selected = _parse_range(range_header, size)
    if selected is None:
        response = minio.client.get_object(minio.bucket, object_name)
        headers["Content-Length"] = str(size)
        return StreamingResponse(_stream(response), headers=headers)

    start, end = selected
    response = minio.client.get_object(
        minio.bucket, object_name, offset=start, length=end - start + 1
    )
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(_stream(response), status_code=206, headers=headers)
