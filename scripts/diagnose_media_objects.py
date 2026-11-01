"""诊断「老录音 MinIO 对象丢失」问题。只读核对，不修改任何数据。

把数据库里每条图片/音频 URL 与 MinIO 里的实际对象表做交叉核对，分成三类，
最后输出 MinIO 全量对象清单（带时间、孤儿标记），便于定位异常 key 或误删：

  OK       现行代理解析逻辑就能命中对象（正常）
  FIXABLE  现行逻辑会 404，但历史 key 形态（裸 key / 双前缀 key）能命中——
           对象其实还在 MinIO 里，部署 media 代理的候选解析修复后恢复
  MISSING  所有候选 key 都不存在——对象真的丢了，需要查 MinIO 数据卷/备份
           和当时的运维操作记录（mc rm / 手工清理 / 孤儿清理脚本误删）

用法（在能同时访问数据库和 MinIO 的机器上，比如服务器）：

    APP_ENV=production venv/bin/python scripts/diagnose_media_objects.py

仓库根目录或 backend/ 目录下运行都可以，脚本会自动定位 app 包；
数据库/对象存储配置按应用自身逻辑加载（APP_ENV 决定读 .env 还是 .env.prod）。
注意：导入 app 配置会按应用自身逻辑确保 bucket 存在（与启动行为一致），
除此之外全程只读。
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
from pydantic import ValidationError

try:
    from app.api.media import resolve_object_candidates
    from app.config import settings
    from app.infrastructure.minio_client import get_minio_client
except ModuleNotFoundError:
    # 从仓库根目录等位置运行：app 包不在 sys.path，自动补上再导入。
    _HERE = Path(__file__).resolve().parent
    for _root in (_HERE.parent / "backend", _HERE.parent):
        if (_root / "app").is_dir():
            sys.path.insert(0, str(_root))
            break
    from app.api.media import resolve_object_candidates
    from app.config import settings
    from app.infrastructure.minio_client import get_minio_client
except ValidationError:
    # 常见于手动运行时 shell 里没有服务的环境变量（如 APP_ENV=production），
    # 读到的是占位配置的 .env。诊断只需要数据库和 MinIO 配置，补齐环境重跑即可。
    print("配置校验失败：手动运行的 shell 缺少服务的环境变量。")
    print("如果服务用 APP_ENV=production 启动（加载 .env.prod），请这样重跑：")
    print("    APP_ENV=production venv/bin/python scripts/diagnose_media_objects.py")
    sys.exit(1)

SAMPLE_LIMIT = 20


def _asyncpg_dsn() -> str:
    return settings.DATABASE_URL.replace("+asyncpg", "", 1)


def _path_from_url(url: str) -> str:
    """把 DB 里存的 URL 还原成媒体代理收到的路径形态。"""
    path = urlparse(url).path.lstrip("/")
    api_media = f"{settings.API_PREFIX.lstrip('/')}/media/"
    return path.removeprefix(api_media)


async def fetch_media_urls(conn) -> list[tuple[str, str, datetime]]:
    """返回 (content_type, url, created_at) 列表。"""
    rows = await conn.fetch(
        """
        SELECT content_type, file_url, image_url, created_at
        FROM records
        WHERE file_url IS NOT NULL OR image_url IS NOT NULL
        ORDER BY created_at
        """
    )
    urls: list[tuple[str, str, datetime]] = []
    for row in rows:
        for url in (row["file_url"], row["image_url"]):
            if url:
                urls.append((row["content_type"], url, row["created_at"]))
    return urls


async def main() -> None:
    minio = get_minio_client()
    print(f"枚举 bucket {minio.bucket!r} 的对象 …")
    existing: dict[str, tuple[datetime, int]] = {}
    for obj in minio.client.list_objects(minio.bucket, recursive=True):
        existing[obj.object_name] = (obj.last_modified, obj.size)
    print(f"MinIO 对象总数: {len(existing)}")

    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        urls = await fetch_media_urls(conn)
    finally:
        await conn.close()
    print(f"数据库媒体 URL 总数: {len(urls)}\n")

    results: Counter[str] = Counter()
    fixable: list[tuple[datetime, str, str, str]] = []
    missing: list[tuple[datetime, str, str]] = []
    referenced: set[str] = set()

    for content_type, url, created_at in urls:
        path = _path_from_url(url)
        candidates = resolve_object_candidates(path)
        referenced.update(candidates)
        if candidates and candidates[0] in existing:
            results["OK"] += 1
        elif any(c in existing for c in candidates):
            results["FIXABLE"] += 1
            hit = next(c for c in candidates if c in existing)
            fixable.append((created_at, content_type, url, hit))
        else:
            results["MISSING"] += 1
            missing.append((created_at, content_type, url))

    print("=== 结果 ===")
    for name in ("OK", "FIXABLE", "MISSING"):
        print(f"{name:8} {results[name]}")

    if fixable:
        print(f"\n=== FIXABLE（部署修复后立即可恢复，前 {SAMPLE_LIMIT} 条）===")
        for created_at, content_type, url, hit in fixable[:SAMPLE_LIMIT]:
            print(f"  {created_at:%Y-%m-%d} [{content_type}] {url}  ->  实际 key {hit}")

    if missing:
        print(f"\n=== MISSING（对象确实丢失，前 {SAMPLE_LIMIT} 条）===")
        for created_at, content_type, url in missing[:SAMPLE_LIMIT]:
            print(f"  {created_at:%Y-%m-%d} [{content_type}] {url}")
        print(
            "\n建议排查方向：MinIO 数据卷是否保住（容器/镜像重建、docker system prune）、"
            "是否有过 bucket 迁移或清空操作、服务器磁盘与备份；同时翻当时时段的运维记录"
            "（mc rm / 手工清理 / 孤儿清理脚本），特别注意带空格或特殊字符的 key 被误删。"
        )

    print("\n=== MinIO 对象清单 ===")
    for key in sorted(existing):
        last_modified, size = existing[key]
        mark = "" if key in referenced else "  <- 孤儿（数据库没有引用它的 URL）"
        print(f"  {last_modified:%Y-%m-%d %H:%M}  {size:>12}  {key}{mark}")

    if not fixable and not missing:
        print("\n所有 URL 都能按现行逻辑命中，对象不存在解析问题。")


if __name__ == "__main__":
    asyncio.run(main())
