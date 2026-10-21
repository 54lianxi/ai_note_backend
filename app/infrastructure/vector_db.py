"""Qdrant 向量数据库客户端"""
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import settings


def record_payload(
    record_id: str,
    user_id: str,
    chunk_index: int | None = None,
) -> dict[str, str | int]:
    """Qdrant 只保存定位记录和隔离用户检索所需的字段。"""
    payload: dict[str, str | int] = {
        "record_id": record_id,
        "user_id": user_id,
    }
    if chunk_index is not None:
        payload["chunk_index"] = chunk_index
    return payload


class VectorDBClient:
    """Qdrant 向量数据库客户端封装"""
    
    def __init__(self):
        if settings.QDRANT_URL:
            # 远程 Qdrant Cloud：带 API Key 走 HTTPS。
            self.client = QdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY or None,
                timeout=30,
            )
        else:
            self.client = QdrantClient(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
            )
        self.collection = settings.QDRANT_COLLECTION
        self._ensure_collection()
    
    def _ensure_collection(self):
        """确保 Collection 和检索所需索引存在"""
        collections = [c.name for c in self.client.get_collections().collections]
        if self.collection not in collections:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=settings.EMBEDDING_DIM,
                    distance=Distance.COSINE,
                ),
            )
        # 按 user_id 过滤检索需要 keyword 索引，缺了会直接 400。
        indexed = self.client.get_collection(self.collection).payload_schema or {}
        if "user_id" not in indexed:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="user_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
        if "record_id" not in indexed:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="record_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )
    
    def upsert_record(
        self,
        vector: list[float],
        record_id: str,
        user_id: str,
    ) -> str:
        """
        插入或更新记录向量
        :param vector: 向量数据
        :param record_id: PostgreSQL 记录 ID，同时作为 Qdrant point ID
        :param user_id: 用户 ID，用于检索时隔离数据
        :return: record_id
        """
        # Qdrant 使用整数或 UUID 作为 point ID
        self.client.upsert(
            collection_name=self.collection,
            points=[
                PointStruct(
                    id=record_id,
                    vector=vector,
                    payload=record_payload(record_id, user_id),
                )
            ],
        )
        return record_id

    def upsert_chunks(
        self,
        chunks: list[dict],
        record_id: str,
        user_id: str,
    ) -> str:
        """写入一条记录的多个文本分块，所有点仍指向同一个 record_id。"""
        points = []
        for index, chunk in enumerate(chunks):
            point_id = (
                record_id
                if index == 0
                else str(uuid.uuid5(uuid.NAMESPACE_URL, f"{record_id}:{index}"))
            )
            points.append(
                PointStruct(
                    id=point_id,
                    vector=chunk["vector"],
                    payload=record_payload(record_id, user_id, chunk_index=index),
                )
            )
        if points:
            self.client.upsert(collection_name=self.collection, points=points)
        return record_id
    
    def search(
        self,
        vector: list[float],
        top_k: int = 5,
        user_id: str | None = None,
        score_threshold: float = 0.5,
    ) -> list[dict]:
        """
        向量相似度检索
        :param vector: 查询向量
        :param top_k: 返回数量
        :param user_id: 用户 ID 过滤（可选）
        :param score_threshold: 最低相似度阈值
        :return: 检索结果列表
        """
        query_filter = None
        if user_id:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="user_id",
                        match=MatchValue(value=user_id),
                    )
                ]
            )
        
        response = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            score_threshold=score_threshold,
            query_filter=query_filter,
        )
        results = response.points
        
        return [
            {
                "id": str(r.id),
                "score": r.score,
                "payload": r.payload,
            }
            for r in results
        ]
    
    def delete(self, point_id: str) -> None:
        """删除向量"""
        self.client.delete(
            collection_name=self.collection,
            points_selector=[point_id],
        )

    def delete_record(self, record_id: str, user_id: str) -> None:
        """删除一条记录的所有正文分块。"""
        self.client.delete(
            collection_name=self.collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="record_id",
                            match=MatchValue(value=record_id),
                        ),
                        FieldCondition(
                            key="user_id",
                            match=MatchValue(value=user_id),
                        ),
                    ]
                )
            ),
        )


# 全局客户端实例（延迟初始化）
_vector_client: VectorDBClient | None = None


def get_vector_client() -> VectorDBClient:
    """获取向量数据库客户端单例"""
    global _vector_client
    if _vector_client is None:
        _vector_client = VectorDBClient()
    return _vector_client
