"""对话消息里「相关笔记」的组装逻辑。"""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.chat import _message_response, _related_record_ids
from app.schemas.record import RecordResponse


def _message(related_ids) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        role="assistant",
        content="海贼王你看到 800 集了",
        content_type="result",
        record_id=None,
        related_record_ids=related_ids,
        created_at=datetime.now(UTC),
    )


def _record_response(record_id: uuid.UUID, content: str) -> RecordResponse:
    return RecordResponse(
        record_id=record_id,
        content=content,
        content_type="text",
        created_at=datetime.now(UTC),
    )


def test_message_without_related_records_has_no_links():
    response = _message_response(_message([]), {})
    assert response.related_records == []
    assert response.related_record_ids == []


def test_message_carries_related_records_in_id_order():
    first, second = uuid.uuid4(), uuid.uuid4()
    related = {
        first: _record_response(first, "海贼王看到 800 集了"),
        second: _record_response(second, "周末计划：去看展"),
    }

    response = _message_response(_message([str(second), str(first)]), related)

    assert [record.record_id for record in response.related_records] == [second, first]
    assert response.related_records[0].content == "周末计划：去看展"


def test_related_records_missing_from_db_are_skipped():
    known, missing = uuid.uuid4(), uuid.uuid4()
    related = {known: _record_response(known, "还在的笔记")}

    response = _message_response(_message([str(known), str(missing)]), related)

    assert [record.record_id for record in response.related_records] == [known]


def test_invalid_ids_are_ignored():
    valid = uuid.uuid4()
    message = _message([str(valid), "not-a-uuid", None])

    assert _related_record_ids(message) == [valid]
    assert _message_response(message, {}).related_records == []
