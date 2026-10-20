import uuid

import pytest
from fastapi import HTTPException

from app.api.report import report_ai_content
from app.models.report import AIReport
from app.schemas.report import AIReportCreate


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDb:
    def __init__(self, message):
        self.message = message
        self.added = []

    async def execute(self, _query):
        return _Result(self.message)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_ai_report_is_saved_for_owned_message():
    db = _FakeDb(object())
    data = AIReportCreate(
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        reason="harmful",
        details="test",
    )

    result = await report_ai_content(data, db, str(uuid.uuid4()))

    assert result["message"] == "举报已提交"
    report = db.added[0]
    assert isinstance(report, AIReport)
    assert report.reason == "harmful"
    assert report.details == "test"


@pytest.mark.asyncio
async def test_ai_report_rejects_missing_message():
    db = _FakeDb(None)
    data = AIReportCreate(
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        reason="other",
    )

    with pytest.raises(HTTPException) as error:
        await report_ai_content(data, db, str(uuid.uuid4()))

    assert error.value.status_code == 404
    assert db.added == []
