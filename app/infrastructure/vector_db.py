"""Qdrant 向量数据库客户端"""
from uuid import uuid4

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import settings


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
    
    def upsert(
        self,
        vector: list[float],
        payload: dict,
        point_id: str | None = None,
    ) -> str:
        """
        插入或更新向量
        :param vector: 向量数据
        :param payload: 附加元数据
        :param point_id: 自定义 ID（可选）
        :return: point_id
        """
        if point_id is None:
            point_id = str(uuid4())
        
        # Qdrant 使用整数或 UUID 作为 point ID
        self.client.upsert(
            collection_name=self.collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )
            ],
        )
        return point_id
    
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


# 全局客户端实例（延迟初始化）
_vector_client: VectorDBClient | None = None


def get_vector_client() -> VectorDBClient:
    """获取向量数据库客户端单例"""
    global _vector_client
    if _vector_client is None:
        _vector_client = VectorDBClient()
    return _vector_client
