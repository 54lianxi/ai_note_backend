"""把本地 Qdrant 的向量数据迁移到远程 Qdrant（例如 Qdrant Cloud）。

用法：
    uv run python scripts/migrate_qdrant.py

默认从 .env 里的 QDRANT_HOST/QDRANT_PORT 读取，写入 QDRANT_URL/QDRANT_API_KEY。
也可以用命令行参数覆盖：

    uv run python scripts/migrate_qdrant.py \
        --source-host localhost --source-port 6333 \
        --target-url https://xxx.cloud.qdrant.io --target-api-key <key>
"""
import argparse

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 Qdrant 向量数据")
    parser.add_argument("--source-host", default=settings.QDRANT_HOST)
    parser.add_argument("--source-port", type=int, default=settings.QDRANT_PORT)
    parser.add_argument("--source-api-key", default="")
    parser.add_argument("--target-url", default=settings.QDRANT_URL)
    parser.add_argument("--target-api-key", default=settings.QDRANT_API_KEY)
    parser.add_argument("--collection", default=settings.QDRANT_COLLECTION)
    parser.add_argument("--dim", type=int, default=settings.EMBEDDING_DIM)
    parser.add_argument("--batch", type=int, default=100)
    return parser.parse_args()


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    existing = [c.name for c in client.get_collections().collections]
    if name not in existing:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"[target] 已创建 collection: {name} (dim={dim})")


def main() -> None:
    args = parse_args()
    if not args.target_url:
        raise SystemExit("缺少目标地址：请设置 QDRANT_URL 或传 --target-url")

    source = QdrantClient(
        host=args.source_host,
        port=args.source_port,
        api_key=args.source_api_key or None,
    )
    target = QdrantClient(
        url=args.target_url,
        api_key=args.target_api_key or None,
        timeout=60,
    )

    source_count = source.count(collection_name=args.collection).count
    print(f"[source] {args.source_host}:{args.source_port}/{args.collection} 共 {source_count} 条")

    ensure_collection(target, args.collection, args.dim)

    migrated = 0
    offset = None
    while True:
        points, offset = source.scroll(
            collection_name=args.collection,
            limit=args.batch,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break

        target.upsert(
            collection_name=args.collection,
            points=[
                PointStruct(id=point.id, vector=point.vector, payload=point.payload)
                for point in points
            ],
        )
        migrated += len(points)
        print(f"[migrate] 已写入 {migrated}/{source_count}")

        if offset is None:
            break

    print(f"[done] 迁移完成，共 {migrated} 条 -> {args.target_url}")


if __name__ == "__main__":
    main()
