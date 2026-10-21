from types import SimpleNamespace

from scripts import compact_qdrant_payloads


def test_compact_qdrant_payloads_overwrites_old_payload(monkeypatch):
    overwritten = []

    class FakeClient:
        def count(self, **_kwargs):
            return SimpleNamespace(count=2)

        def scroll(self, **_kwargs):
            return (
                [
                    SimpleNamespace(
                        id="11111111-1111-1111-1111-111111111111",
                        payload={
                            "record_id": "11111111-1111-1111-1111-111111111111",
                            "user_id": "user-1",
                            "content": "旧文本副本",
                            "summary": "旧摘要",
                        },
                    ),
                    SimpleNamespace(
                        id="22222222-2222-2222-2222-222222222222",
                        payload={
                            "user_id": "user-2",
                            "content": "另一份旧文本",
                        },
                    ),
                ],
                None,
            )

        def overwrite_payload(self, **kwargs):
            overwritten.append(kwargs)

    monkeypatch.setattr(
        compact_qdrant_payloads,
        "parse_args",
        lambda: SimpleNamespace(collection="records", batch=100),
    )
    monkeypatch.setattr(
        compact_qdrant_payloads,
        "get_vector_client",
        lambda: SimpleNamespace(client=FakeClient()),
    )

    compact_qdrant_payloads.main()

    assert [call["payload"] for call in overwritten] == [
        {
            "record_id": "11111111-1111-1111-1111-111111111111",
            "user_id": "user-1",
        },
        {
            "record_id": "22222222-2222-2222-2222-222222222222",
            "user_id": "user-2",
        },
    ]
