"""本地文件夹存储：图片/附件/录音等静态文件直接落盘，替代 MinIO。

文件写入 settings.STATIC_FILES_DIR，对外通过 main.py 挂载的
STATIC_FILES_URL_PREFIX 静态路由公开访问，URL 形如
`/static/files/<uuid>.mp4`。旧数据里指向 MinIO 的 URL 不受影响。
"""

import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)


def _ensure_dir() -> Path:
    directory = Path(settings.STATIC_FILES_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _prefix() -> str:
    return settings.STATIC_FILES_URL_PREFIX.rstrip("/")


def _pick_ext(
    filename: str | None, content_type: str, fallback_ext: str = ""
) -> str:
    """优先用原始文件名的后缀，其次按 Content-Type 猜，保证 URL 可直接播放/打开。"""
    suffix = Path(filename or "").suffix
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "") or ""
    return guessed or fallback_ext


def _public_url(object_name: str) -> str:
    return f"{_prefix()}/{object_name}"


def save_bytes(
    data: bytes, content_type: str, filename: str | None = None
) -> str:
    """保存内存中的文件数据，返回公开访问 URL。"""
    directory = _ensure_dir()
    ext = _pick_ext(filename, content_type)
    name = f"{uuid.uuid4().hex}{ext}"
    (directory / name).write_bytes(data)
    return _public_url(name)


def save_from_path(
    path: str, content_type: str, filename: str | None = None
) -> str:
    """把临时文件搬进存储目录（move 免二次拷贝，长录音友好），返回公开 URL。"""
    directory = _ensure_dir()
    ext = _pick_ext(filename, content_type, fallback_ext=Path(path).suffix)
    name = f"{uuid.uuid4().hex}{ext}"
    shutil.move(path, directory / name)
    return _public_url(name)


def delete_files_by_urls(urls: list[str | None]) -> None:
    """按 URL 删除本地文件；不属于本存储前缀的（如旧 MinIO 记录）跳过。"""
    prefix_path = _prefix().lstrip("/") + "/"
    directory = _ensure_dir().resolve()
    for url in urls:
        if not url:
            continue
        path_part = urlparse(url).path.lstrip("/")
        if not path_part.startswith(prefix_path):
            continue
        target = (directory / path_part[len(prefix_path):]).resolve()
        if target.is_relative_to(directory) and target.exists():
            try:
                target.unlink()
            except OSError:
                logger.warning("本地静态文件删除失败: %s", target, exc_info=True)
