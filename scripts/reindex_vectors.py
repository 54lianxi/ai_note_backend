"""用当前配置的 embedding 模型重新向量化所有记录。

换 embedding 厂商或模型后必须跑一次：向量空间变了，
旧向量和新查询向量算出来的相似度没有意义，检索会召回错东西。

用法：
    uv run python scripts/reindex_vectors.py

默认按 .env 里的 DATABASE_URL 和 QDRANT_* 连接，
也可以先用 --dry-run 看看会处理多少条。
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from app.config import settings  # noqa: E402
from app.infrastructure.vector_db import get_vector_client  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402

# asyncpg 不认 SQLAlchemy 的 driver 前缀和 asyncpg 方言参数。
_SELECT = """
    SELECT id, user_id, content, summary, content_type, created_at
    FROM records
    ORDER BY created_at
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重新向量化所有记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计条数，不写入")
    parser.add_argument("--batch", type=int, default=20, help="每批处理条数")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    dsn = settings.DATABASE_URL.replace("+asyncpg", "")

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(_SELECT)
    finally:
        await conn.close()

    print(f"待处理记录：{len(rows)} 条")
    print(f"embedding：{settings.EMBEDDING_MODEL}（{settings.EMBEDDING_DIM} 维）")
    if args.dry_run or not rows:
        return 0

    vector_db = get_vector_client()
    embedding = EmbeddingService()
    ok = failed = 0

    for index, row in enumerate(rows, 1):
        record_id = str(row["id"])
        try:
            vector = await embedding.embed(row["content"])
            vector_db.upsert(
                vector=vector,
                payload={
                    "record_id": record_id,
                    "user_id": str(row["user_id"]),
                    "content": row["content"],
                    "summary": row["summary"],
                    "content_type": row["content_type"],
                    "created_at": row["created_at"].isoformat(),
                },
                point_id=record_id,
            )
            ok += 1
            print(f"  [{index}/{len(rows)}] ok   {row['content'][:30]}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  [{index}/{len(rows)}] fail {record_id}: {error}")

        if index % args.batch == 0:
            await asyncio.sleep(0.5)

    print(f"完成：成功 {ok}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
