from types import SimpleNamespace

from qdrant_client.models import FilterSelector

from app.infrastructure.vector_db import VectorDBClient, record_payload


def test_record_payload_contains_only_lookup_and_filter_fields():
    assert record_payload("record-1", "user-1") == {
        "record_id": "record-1",
        "user_id": "user-1",
    }


def test_upsert_record_uses_record_id_and_minimal_payload():
    calls = []

    class FakeQdrantClient:
        def upsert(self, **kwargs):
            calls.append(kwargs)

    vector_db = VectorDBClient.__new__(VectorDBClient)
    vector_db.client = FakeQdrantClient()
    vector_db.collection = "records"

    result = vector_db.upsert_record(
        vector=[0.1, 0.2],
        record_id="11111111-1111-1111-1111-111111111111",
        user_id="00000000-0000-0000-0000-000000000001",
    )

    point = calls[0]["points"][0]
    assert result == "11111111-1111-1111-1111-111111111111"
    assert point.id == result
    assert point.vector == [0.1, 0.2]
    assert point.payload == {
        "record_id": result,
        "user_id": "00000000-0000-0000-0000-000000000001",
    }


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


def test_upsert_chunks_uses_stable_ids_and_chunk_indexes():
    calls = []

    class FakeQdrantClient:
        def upsert(self, **kwargs):
            calls.append(kwargs)

    vector_db = VectorDBClient.__new__(VectorDBClient)
    vector_db.client = FakeQdrantClient()
    vector_db.collection = "records"
    record_id = "11111111-1111-1111-1111-111111111111"

    result = vector_db.upsert_chunks(
        chunks=[
            {"text": "第一段", "vector": [0.1]},
            {"text": "第二段", "vector": [0.2]},
        ],
        record_id=record_id,
        user_id="user-1",
    )

    points = calls[0]["points"]
    assert result == record_id
    assert points[0].id == record_id
    assert points[1].id != record_id
    assert [point.payload["chunk_index"] for point in points] == [0, 1]
    assert all(point.payload["record_id"] == record_id for point in points)


def test_delete_record_removes_all_chunks_by_record_and_user():
    calls = []

    class FakeQdrantClient:
        def delete(self, **kwargs):
            calls.append(kwargs)

    vector_db = VectorDBClient.__new__(VectorDBClient)
    vector_db.client = FakeQdrantClient()
    vector_db.collection = "records"

    vector_db.delete_record("record-1", "user-1")

    selector = calls[0]["points_selector"]
    assert isinstance(selector, FilterSelector)
    assert len(selector.filter.must) == 2
