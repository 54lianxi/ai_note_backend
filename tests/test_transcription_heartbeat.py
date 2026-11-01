"""后台转写的失联判定：长录音靠心跳，不靠固定超时。"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.api import record as record_api
from app.config import settings
from app.services.asr_service import ASRServiceError
from app.workflows import record_workflow as record_module

_RECORD_ID = "11111111-1111-1111-1111-111111111111"


class FakeRow:
    """转写状态的内存替身，避免 ORM 类的描述符语义干扰断言。"""

    def __init__(
        self,
        *,
        status: str = "pending",
        scheduled_minutes_ago: float = 30,
        heartbeat_minutes_ago: float | None = None,
        error: str | None = None,
    ):
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.content = ""
        self.summary = None
        self.content_type = "voice"
        self.image_url = None
        self.image_key_info = None
        self.file_url = None
        self.file_name = "voice.wav"
        self.file_content_type = "audio/wav"
        self.file_size = 1
        self.transcription_status = status
        self.transcription_error = error
        self.transcription_heartbeat_at = (
            None
            if heartbeat_minutes_ago is None
            else now - timedelta(minutes=heartbeat_minutes_ago)
        )
        self.manual_transcription_count = 0
        self.created_at = now - timedelta(minutes=scheduled_minutes_ago)
        self.updated_at = now - timedelta(minutes=scheduled_minutes_ago)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, rows=(), record=None):
        self.rows = list(rows)
        self.record = record
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    async def get(self, _model, _key):
        return self.record

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


@pytest.mark.asyncio
async def test_heartbeat_write_touches_only_heartbeat_column(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        record_module, "async_session_factory", _SessionFactory(session)
    )

    await record_module._write_transcription_heartbeat(_RECORD_ID)

    sql = str(
        session.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    )
    assert "transcription_heartbeat_at" in sql
    assert "transcription_status" not in sql
    assert session.commits == 1


def test_long_running_transcription_with_heartbeat_is_not_stalled():
    row = FakeRow(scheduled_minutes_ago=120, heartbeat_minutes_ago=0.5)
    assert record_api._heartbeat_stalled(row, datetime.now(UTC)) is False
    assert record_api._transcription_display(row) == ("pending", None)


def test_missing_heartbeat_after_grace_is_stalled():
    row = FakeRow(scheduled_minutes_ago=30, heartbeat_minutes_ago=None)
    assert record_api._transcription_display(row) == ("failed", "stalled")


def test_silenced_heartbeat_is_stalled():
    row = FakeRow(scheduled_minutes_ago=20, heartbeat_minutes_ago=10)
    assert record_api._heartbeat_stalled(row, datetime.now(UTC)) is True


def test_freshly_scheduled_task_without_heartbeat_is_not_stalled():
    row = FakeRow(scheduled_minutes_ago=0.5, heartbeat_minutes_ago=None)
    assert record_api._heartbeat_stalled(row, datetime.now(UTC)) is False


def test_finished_transcription_keeps_stored_status_and_reason():
    row = FakeRow(status="failed", error="timeout", heartbeat_minutes_ago=60)
    assert record_api._transcription_display(row) == ("failed", "timeout")

    assert record_api._transcription_display(FakeRow(status="done")) == ("done", None)


def test_read_response_does_not_mutate_stored_state():
    """读接口只改响应，不再把误判的 failed 写回数据库。"""
    row = FakeRow(scheduled_minutes_ago=30, heartbeat_minutes_ago=None)

    response = record_api._record_response(row)

    assert response.transcription_status == "failed"
    assert response.transcription_error == "stalled"
    assert row.transcription_status == "pending"
    assert row.transcription_error is None


@pytest.mark.asyncio
async def test_get_record_polling_does_not_fail_a_live_task():
    """前端每 4 秒轮询状态，不该把还在跑的长录音转写判死。"""
    row = FakeRow(scheduled_minutes_ago=30, heartbeat_minutes_ago=0.5)
    session = FakeSession(rows=[row])

    response = await record_api.get_record(row.id, session, str(row.user_id), "zh")

    assert response.transcription_status == "pending"
    assert row.transcription_status == "pending"
    assert session.commits == 0


def test_stale_budget_is_wider_than_a_heartbeat_gap():
    """失联阈值要明显宽于心跳间隔，别把一次抖动判成死亡。"""
    interval = settings.TRANSCRIPTION_HEARTBEAT_SECONDS
    stale = record_api._stale_transcription_seconds()
    assert stale == interval * settings.TRANSCRIPTION_HEARTBEAT_MISS_LIMIT + interval
    assert stale > interval * 2


def test_asr_reason_codes():
    assert record_api.asr_reason_code(ASRServiceError("请求超时 timed out")) == "timeout"
    assert (
        record_api.asr_reason_code(
            ASRServiceError("限流", status_code=429, retryable=True)
        )
        == "rate_limited"
    )
    assert (
        record_api.asr_reason_code(ASRServiceError("上游炸了", status_code=503))
        == "unavailable"
    )
    assert (
        record_api.asr_reason_code(ASRServiceError("音频太长", status_code=413))
        == "failed"
    )
    # 没有状态码的本地失败（连接异常、响应结构不对）引导用户稍后重试。
    assert record_api.asr_reason_code(ASRServiceError("上传音频失败")) == "unavailable"
    assert record_api.asr_reason_code(RuntimeError("boom")) == "failed"


def test_httpx_timeout_is_reported_as_timeout():
    """httpx 的超时 str() 为空，只能靠异常链识别。"""
    wrapped = ASRServiceError("语音识别请求失败: ", retryable=True)
    wrapped.__cause__ = httpx.ReadTimeout("")

    assert record_api.asr_reason_code(wrapped) == "timeout"


@pytest.mark.asyncio
async def test_reaper_persists_stalled_pending_rows(monkeypatch):
    rows = [FakeRow(scheduled_minutes_ago=60, heartbeat_minutes_ago=20)]
    session = FakeSession(rows=rows)
    monkeypatch.setattr(record_api, "async_session_factory", _SessionFactory(session))

    count = await record_api.reap_stalled_transcriptions()

    assert count == 1
    assert rows[0].transcription_status == "failed"
    assert rows[0].transcription_error == "stalled"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_reaper_keeps_alive_transcriptions_untouched(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(record_api, "async_session_factory", _SessionFactory(session))

    count = await record_api.reap_stalled_transcriptions()

    assert count == 0
    assert session.commits == 0


@pytest.mark.asyncio
async def test_reaper_sql_filters_on_heartbeat(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(record_api, "async_session_factory", _SessionFactory(session))

    await record_api.reap_stalled_transcriptions()

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "transcription_heartbeat_at" in sql
    assert "transcription_status" in sql


def test_alive_heartbeat_survives_a_two_hour_recording():
    """两小时录音转十几分钟很常见：只要心跳在，就不能被判失败。"""
    row = FakeRow(scheduled_minutes_ago=120, heartbeat_minutes_ago=0.5)

    assert record_api._transcription_display(row) == ("pending", None)
    assert record_api._heartbeat_stalled(row, datetime.now(UTC)) is False


@pytest.mark.asyncio
async def test_background_transcription_reports_failure_reason(monkeypatch, tmp_path):
    class FakeAsr:
        async def transcribe_with_summary_file(self, _path, _filename, **_kwargs):
            await asyncio.sleep(0)  # 让心跳循环先跑一轮
            raise ASRServiceError("识别超时 timed out")

    heartbeats = []

    async def fake_heartbeat(_record_id):
        heartbeats.append(datetime.now(UTC))

    record = FakeRow(error="stalled", heartbeat_minutes_ago=1)
    session = FakeSession(record=record)
    monkeypatch.setattr(record_module, "ASRService", FakeAsr)
    monkeypatch.setattr(record_module, "_write_transcription_heartbeat", fake_heartbeat)
    monkeypatch.setattr(
        record_module, "async_session_factory", _SessionFactory(session)
    )
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"audio")

    await record_module.transcribe_voice_record(
        record_id=_RECORD_ID,
        user_id="00000000-0000-0000-0000-000000000001",
        file_path=str(audio_path),
        filename="audio.wav",
        reason_mapper=record_api.asr_reason_code,
    )

    assert heartbeats, "转写任务必须至少刷一次心跳"
    assert record.transcription_status == "failed"
    assert record.transcription_error == "timeout"
    assert record.transcription_heartbeat_at is None
