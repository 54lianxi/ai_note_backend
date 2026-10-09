from types import SimpleNamespace

from app.infrastructure.vector_db import VectorDBClient


def test_search_uses_qdrant_query_points():
    calls = []

    class FakeQdrantClient:
        def query_points(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                points=[
                    SimpleNamespace(
                        id="11111111-1111-1111-1111-111111111111",
                        score=0.92,
                        payload={"record_id": "record-1"},
                    )
                ]
            )

    vector_db = VectorDBClient.__new__(VectorDBClient)
    vector_db.client = FakeQdrantClient()
    vector_db.collection = "records"

    results = vector_db.search(
        vector=[0.1, 0.2, 0.3],
        top_k=3,
        user_id="00000000-0000-0000-0000-000000000001",
    )

    assert calls[0]["collection_name"] == "records"
    assert calls[0]["query"] == [0.1, 0.2, 0.3]
    assert calls[0]["limit"] == 3
    assert results == [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "score": 0.92,
            "payload": {"record_id": "record-1"},
        }
    ]
