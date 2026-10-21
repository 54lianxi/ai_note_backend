"""清理 Qdrant 存量 payload，只保留 record_id 和 user_id。

不会重新向量化，也不会改动 PostgreSQL。建议在切换到精简 payload 后执行一次：

    uv run python scripts/compact_qdrant_payloads.py
"""
import argparse

from app.config import settings
from app.infrastructure.vector_db import get_vector_client, record_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="精简 Qdrant payload")
    parser.add_argument("--collection", default=settings.QDRANT_COLLECTION)
    parser.add_argument("--batch", type=int, default=100)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    vector_db = get_vector_client()
    client = vector_db.client
    total = client.count(collection_name=args.collection).count
    print(f"待处理向量：{total} 条")

    updated = 0
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=args.collection,
            limit=args.batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            break

        for point in points:
            payload = point.payload or {}
            user_id = payload.get("user_id")
            if not user_id:
                raise SystemExit(f"point {point.id} 缺少 user_id，已停止清理")
            client.overwrite_payload(
                collection_name=args.collection,
                points=[point.id],
                payload=record_payload(
                    str(payload.get("record_id") or point.id),
                    str(user_id),
                ),
            )

        updated += len(points)
        print(f"已处理 {updated}/{total}")
        if offset is None:
            break

    print(f"完成：{updated} 条 payload 已精简")


if __name__ == "__main__":
    main()
