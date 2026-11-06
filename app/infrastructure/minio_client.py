"""MinIO 对象存储客户端"""
import io
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import certifi
import urllib3
from minio import Minio
from minio.error import S3Error
from urllib3.util.retry import Retry
from urllib3.util.timeout import Timeout

from app.config import settings

logger = logging.getLogger(__name__)

# MinIO SDK 自带的默认超时是 5 分钟，对象存储一挂就要占住连接这么久，所以自己接管。
_CONNECT_TIMEOUT = 5.0
# 单次 socket 读取上限；大文件分块上传时计时会重置，不影响慢速上传。
_READ_TIMEOUT = 30.0


def _build_http_client() -> urllib3.PoolManager:
    """带超时的连接池。

    read=False：请求体发到一半时不重传，否则几 M 的附件会被重复上传一次。
    """
    return urllib3.PoolManager(
        timeout=Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        maxsize=10,
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        retries=Retry(
            total=3,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
            read=False,
        ),
    )


class MinIOClient:
    """MinIO 客户端封装"""

    def __init__(self):
        self.client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
            http_client=_build_http_client(),
        )
        self.bucket = settings.MINIO_BUCKET
        self._ensure_bucket()

    def _ensure_bucket(self):
        """确保 Bucket 存在，并允许匿名只读（客户端要能直接播放/预览对象）"""
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            self._ensure_public_read()
        except S3Error as e:
            raise RuntimeError(f"MinIO bucket 初始化失败: {e}")

    def _ensure_public_read(self):
        """给 Bucket 设置只读公开策略。

        对象 URL 会被前端直接用于 <img>/音频播放，不带签名，所以必须公开可读，
        否则匿名 GET 会返回 403 AccessDenied。
        """
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{self.bucket}/*"],
                }
            ],
        }
        self.client.set_bucket_policy(self.bucket, json.dumps(policy))

    def _make_object_name(
        self, filename: str | None, content_type: str, fallback_ext: str = ""
    ) -> str:
        """生成对象 key，一律 records/UUID.ext。

        客户端传来的文件名可能带空格、中文等字符（如 "voice 1.m4a"），直接进
        key 会在播放代理、运维脚本、手工操作里出兼容问题（生产上就丢过这种
        带空格的对象）。原始文件名存在记录的 file_name 字段，只用于展示。
        """
        ext = Path(filename).suffix if filename else ""
        ext = ext or fallback_ext or self._get_extension(content_type)
        return f"records/{uuid4()}{ext}"

    def upload_file(self, file_data: bytes, content_type: str, filename: str | None = None) -> str:
        """
        上传文件到 MinIO
        :param file_data: 文件字节数据
        :param content_type: MIME 类型
        :param filename: 原始文件名，仅用于推断扩展名，不进 key
        :return: 文件访问 URL
        """
        object_name = self._make_object_name(filename, content_type)

        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(file_data),
            length=len(file_data),
            content_type=content_type,
        )

        return self.get_url(object_name)

    def upload_file_from_path(
        self,
        path: str,
        content_type: str,
        filename: str | None = None,
    ) -> str:
        """从磁盘流式上传，长录音不经过内存。key 生成规则同 upload_file。"""
        object_name = self._make_object_name(filename, content_type, fallback_ext=Path(path).suffix)
        size = Path(path).stat().st_size

        with open(path, "rb") as source:
            self.client.put_object(
                self.bucket,
                object_name,
                source,
                length=size,
                content_type=content_type,
            )

        return self.get_url(object_name)

    def get_url(self, object_name: str) -> str:
        """获取对象访问 URL"""
        scheme = "https" if settings.MINIO_SECURE else "http"
        return f"{scheme}://{settings.MINIO_ENDPOINT}/{self.bucket}/{object_name}"

    def delete_file(self, object_name: str) -> None:
        """删除文件"""
        self.client.remove_object(self.bucket, object_name)

    def delete_files_by_url(self, urls: Iterable[str | None]) -> None:
        """按对象 URL 批量删除文件。

        同步阻塞方法，async 调用方请用 asyncio.to_thread 包一层。单个失败只记
        日志，不中断整批：清不掉的文件留给运维处理，不该卡住用户的删除请求。
        """
        for url in urls:
            if not url:
                continue
            try:
                object_name = object_name_from_url(self.bucket, url)
                if object_name:
                    self.delete_file(object_name)
            except Exception:
                logger.warning("删除记录文件失败: %s", url, exc_info=True)

    @staticmethod
    def _get_extension(content_type: str) -> str:
        """根据 MIME 类型获取文件扩展名"""
        mapping = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
        }
        return mapping.get(content_type, ".bin")


# 全局客户端实例（延迟初始化）
_minio_client: MinIOClient | None = None


def get_minio_client() -> MinIOClient:
    """获取 MinIO 客户端单例"""
    global _minio_client
    if _minio_client is None:
        _minio_client = MinIOClient()
    return _minio_client


def object_name_from_url(bucket: str, url: str) -> str | None:
    """从对象存储 URL 反推 MinIO 的 object key，取不到时返回 None。

    get_url 生成的 URL 形如 http://host/{bucket}/{key}，取第一个 /{bucket}/
    之后的部分即 key；外域 URL（CDN 等）不带该标记，返回 None。
    """
    marker = f"/{bucket}/"
    index = url.find(marker)
    if index == -1:
        return None
    return url[index + len(marker):]
