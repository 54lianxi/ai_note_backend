"""媒体代理接口

对象存储（MinIO）只在服务器本机监听，客户端拿到的直连地址（localhost:9000）
在手机上根本连不上，所以图片和音频统一从 API 域名转发出去。音频播放器拖动
进度会发 Range 请求，这里一并支持。
"""
import asyncio
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


def resolve_object_candidates(path: str, bucket: str | None = None) -> list[str]:
    """按尝试顺序返回 URL 路径对应的候选对象 key。

    历史数据有两种 key 形态：新数据 key 带 records/ 子目录（URL 路径
    /records/records/voice_x.wav），老数据是裸 key（URL 路径
    /records/voice_x.wav）。路径只有一层时两种形态无法静态区分，所以都列
    出来，由调用方按顺序 stat/get，取第一个命中的；最后兜底再把路径本身当
    完整 key 试一次（覆盖双前缀 key 的异常形态）。
    """
    bucket = bucket or settings.MINIO_BUCKET
    prefix = f"{bucket}/"
    candidates: list[str] = []
    primary = (
        path[len(prefix):]
        if path.startswith(prefix) and path.count("/") >= 2
        else path
    )
    if primary:
        candidates.append(primary)
    if path.startswith(prefix):
        stripped = path[len(prefix):]
        if stripped and stripped not in candidates:
            candidates.append(stripped)
    if path and path not in candidates:
        candidates.append(path)
    return candidates


def resolve_object_name(path: str, bucket: str | None = None) -> str:
    """把 URL 路径还原成最可能的对象存储 key（完整候选见 resolve_object_candidates）。"""
    candidates = resolve_object_candidates(path)
    return candidates[0] if candidates else ""


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


def _stat_first_object(
    minio, candidates: list[str]
) -> tuple[object | None, str | None]:
    """按候选顺序取第一个存在的对象，取不到返回 (None, None)。

    历史数据 key 形态不统一，所以要逐个试；urllib3 是同步库，整个循环必须放在
    线程里跑，否则一次对象存储抖动就把唯一的事件循环冻住、所有接口一起超时。
    """
    for candidate in candidates:
        try:
            stat = minio.client.stat_object(minio.bucket, candidate)
        except S3Error:
            continue
        return stat, candidate
    return None, None


@router.get("/media/{path:path}")
async def get_media(
    path: str,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
):
    """读取图片/音频对象。公开访问，和原来对象存储的只读策略一致。"""
    candidates = resolve_object_candidates(path)
    minio = get_minio_client()
    stat, object_name = await asyncio.to_thread(_stat_first_object, minio, candidates)
    if stat is None:
        logger.info("媒体对象不存在: %s (候选 %s)", path, candidates)
        raise HTTPException(status_code=404, detail="文件不存在")

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
        response = await asyncio.to_thread(minio.client.get_object, minio.bucket, object_name)
        headers["Content-Length"] = str(size)
        return StreamingResponse(_stream(response), headers=headers)

    start, end = selected
    response = await asyncio.to_thread(
        minio.client.get_object,
        minio.bucket,
        object_name,
        offset=start,
        length=end - start + 1,
    )
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(_stream(response), status_code=206, headers=headers)
