"""记录上传和记录管理接口"""

import asyncio
import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from minio.error import S3Error
from openai import APITimeoutError, AuthenticationError, OpenAIError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.media import resolve_object_candidates
from app.config import settings
from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId
from app.core.timeutil import ensure_utc, parse_utc
from app.infrastructure.database import async_session_factory, get_db
from app.infrastructure.minio_client import get_minio_client
from app.infrastructure.vector_db import get_vector_client
from app.models.record import Record
from app.schemas.record import (
    RecordListResponse,
    RecordResponse,
    RecordUpdate,
    TextRecordCreate,
)
from app.services.asr_service import ASRServiceError
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.subscription_service import (
    FREE_FILE_NOTE_LIMIT,
    FREE_IMAGE_NOTE_LIMIT,
    FREE_VOICE_NOTE_LIMIT,
    get_state,
    is_subscribed,
)
from app.workflows.record_workflow import (
    embed_text_chunks,
    execute_record_workflow,
    transcribe_voice_record,
)

router = APIRouter(tags=["记录管理"])
logger = logging.getLogger(__name__)
DbSession = Annotated[AsyncSession, Depends(get_db)]


class _UploadTooLarge(Exception):
    """流式拷贝时超过兜底体积上限。"""


def _copy_upload_to_temp(file: UploadFile, max_bytes: int) -> tuple[str, int]:
    """把上传内容流式写到临时文件，长录音不占进程内存。"""
    suffix = Path(file.filename or "").suffix or ".audio"
    fd, path = tempfile.mkstemp(prefix="voice-", suffix=suffix)
    size = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise _UploadTooLarge
                target.write(chunk)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path, size


async def _spool_voice_upload(
    file: UploadFile,
    max_mb: int,
    language: str | None,
) -> tuple[str, int]:
    """语音落临时文件，返回路径和字节数。上限只作防滥用兜底，不展示给用户。"""
    max_bytes = max_mb * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=t(language, "upload_too_large", limit=max_mb),
        )
    try:
        return await asyncio.to_thread(_copy_upload_to_temp, file, max_bytes)
    except _UploadTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail=t(language, "upload_too_large", limit=max_mb),
        ) from exc


def _remove_temp_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


async def _read_upload(file: UploadFile, max_mb: int, language: str | None) -> bytes:
    """读取上传内容，并在进入对象存储/ASR 前拦截超大文件。"""
    max_bytes = max_mb * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=t(language, "upload_too_large", limit=max_mb),
        )
    data = bytearray()
    while len(data) <= max_bytes:
        chunk = await file.read(min(1024 * 1024, max_bytes + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=t(language, "upload_too_large", limit=max_mb),
        )
    return bytes(data)


def _model_auth_error(language: str | None) -> HTTPException:
    return HTTPException(status_code=502, detail=t(language, "model_auth_failed"))


def _model_service_error(error: Exception, language: str | None) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=t(language, "model_service_failed", error=error),
    )


def _record_response(
    record: Record, message: str | None = None, language: str | None = None
) -> RecordResponse:
    message = message or t(language, "record_saved")
    display_status, display_error = _transcription_display(record)
    return RecordResponse(
        record_id=record.id,
        message=message,
        content=record.content,
        summary=record.summary,
        content_type=record.content_type,
        image_url=record.image_url,
        image_key_info=record.image_key_info,
        file_url=record.file_url,
        file_name=record.file_name,
        file_content_type=record.file_content_type,
        file_size=record.file_size,
        transcription_status=display_status,
        transcription_error=display_error,
        manual_transcription_count=record.manual_transcription_count,
        manual_transcription_limit=settings.MANUAL_TRANSCRIPTION_LIMIT,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _stale_transcription_seconds() -> int:
    """心跳失联多久算任务丢失。留够余量，别把 GC 或网络抖动误判成死亡。"""
    interval = settings.TRANSCRIPTION_HEARTBEAT_SECONDS
    return interval * settings.TRANSCRIPTION_HEARTBEAT_MISS_LIMIT + interval


def _heartbeat_stalled(record: Record, now: datetime) -> bool:
    """判断后台转写是否失联；只读，不改 ORM 对象。

    心跳由转写任务定期刷新，所以长录音只要还在跑就不会被误判。
    任务丢失时 pending 行不再有心跳，超过阈值即视为失败。
    """
    if (record.transcription_status or "done") != "pending":
        return False
    grace = timedelta(seconds=settings.TRANSCRIPTION_START_GRACE_SECONDS)
    stale = timedelta(seconds=_stale_transcription_seconds())
    scheduled_at = record.updated_at or record.created_at
    if scheduled_at is None:
        return False
    if ensure_utc(scheduled_at) > now - grace:
        # 刚调度的任务还没来得及发第一次心跳，不能算失联。
        return False
    heartbeat_at = record.transcription_heartbeat_at
    if heartbeat_at is None:
        return True
    return ensure_utc(heartbeat_at) < now - stale


def _transcription_display(record: Record) -> tuple[str, str | None]:
    """返回展示用的转写状态和原因码。

    读接口以前会把判定为超时的 pending 直接写成 failed 并提交，
    前端每 4 秒的状态轮询自己就能把还在正常跑的长录音转写判死。
    这里只改响应，真正落库交给 [reap_stalled_transcriptions]。
    """
    status = record.transcription_status or "done"
    error = record.transcription_error
    if status == "pending" and _heartbeat_stalled(record, datetime.now(UTC)):
        return "failed", "stalled"
    if status == "done":
        return status, None
    return status, error


_TIMEOUT_EXCEPTION_NAMES = {"APITimeoutError", "TimeoutException", "ReadTimeout"}


def _is_timeout(error: BaseException) -> bool:
    """沿异常链识别超时；httpx 超时的 str() 是空串，只能按类型判断。"""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.TimeoutException, APITimeoutError)):
            return True
        if type(current).__name__ in _TIMEOUT_EXCEPTION_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def asr_reason_code(error: Exception) -> str:
    """把 ASR 异常归一成给客户端展示的原因码。"""
    if _is_timeout(error) or "timed out" in str(error).lower():
        return "timeout"
    if isinstance(error, ASRServiceError):
        status = error.status_code
        if status == 429:
            return "rate_limited"
        if status is None or status >= 500:
            # 连接失败、上游 5xx 或响应结构不对，都引导用户稍后重试。
            return "unavailable"
        return "failed"
    return "failed"


async def reap_stalled_transcriptions() -> int:
    """把心跳失联的后台转写落库为失败。

    后台转写跑在 API 进程内，重启会杀掉正在执行的任务，音频已存但没人再
    推进；不能让用户永远看到「正在转写」，所以由巡检任务负责回收。
    """
    now = datetime.now(UTC)
    grace = timedelta(seconds=settings.TRANSCRIPTION_START_GRACE_SECONDS)
    stale = timedelta(seconds=_stale_transcription_seconds())
    heartbeat = Record.transcription_heartbeat_at
    scheduled_at = func.coalesce(Record.updated_at, Record.created_at)
    async with async_session_factory() as session:
        result = await session.execute(
            select(Record).where(
                Record.transcription_status == "pending",
                or_(
                    and_(heartbeat.is_not(None), heartbeat < now - stale),
                    and_(heartbeat.is_(None), scheduled_at < now - grace),
                ),
            )
        )
        rows = list(result.scalars().all())
        for record in rows:
            record.transcription_status = "failed"
            record.transcription_error = "stalled"
        if rows:
            await session.commit()
    if rows:
        logger.warning("后台转写心跳失联，标记为失败：%d 条", len(rows))
    return len(rows)


async def run_stalled_transcription_reaper() -> None:
    """周期性回收失联的后台转写任务。"""
    interval = settings.TRANSCRIPTION_REAPER_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        try:
            await reap_stalled_transcriptions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("巡检失联转写任务失败", exc_info=True)


def _workflow_response(result, language: str | None = None) -> RecordResponse:
    created_at = parse_utc(result.created_at) or datetime.now(UTC)
    if result.transcription_status == "pending":
        message = t(language, "record_saved_transcribing")
    else:
        message = t(
            language,
            "record_saved" if not result.error else "record_saved_no_vector",
        )
    return RecordResponse(
        record_id=uuid.UUID(result.record_id),
        message=message,
        content=result.content,
        summary=result.summary,
        content_type=result.content_type,
        image_url=result.image_url or None,
        image_key_info=str(result.image_key_info) if result.image_key_info else None,
        file_url=result.file_url or None,
        file_name=result.filename or None,
        file_content_type=result.content_type_header or None,
        file_size=len(result.file_data) if result.file_data else None,
        transcription_status=result.transcription_status or "done",
        transcription_error=None,
        manual_transcription_count=0,
        manual_transcription_limit=settings.MANUAL_TRANSCRIPTION_LIMIT,
        created_at=created_at,
        updated_at=parse_utc(result.updated_at) or created_at,
    )


@router.post("/records/text", response_model=RecordResponse)
async def create_text_record(
    data: TextRecordCreate,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """上传文本记录"""
    try:
        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=str(data.conversation_id or ""),
            content_type="text",
            content=data.content,
            language=language,
        )
        return _workflow_response(result, language)
    except AuthenticationError as e:
        logger.exception("创建文本记录时模型服务认证失败")
        raise _model_auth_error(language) from e
    except OpenAIError as e:
        logger.exception("创建文本记录时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("创建文本记录失败")
        raise HTTPException(
            status_code=500, detail=t(language, "record_failed", error=e)
        ) from e


@router.post("/records/voice", response_model=RecordResponse)
async def create_voice_record(
    file: Annotated[UploadFile, File(...)],
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
    background_tasks: BackgroundTasks,
    conversation_id: Annotated[str | None, Form()] = None,
    duration_seconds: Annotated[int, Form(ge=0)] = 0,
):
    """上传语音记录。

    保存请求只做存储：先把记录落库并返回，转写交给后台任务用整段音频完成。
    这样实时转写断开或长录音识别不完整时，最终笔记内容仍然完整。
    保存接口不再把 ASR 耗时算进「保存笔记」。

    免费用户最多保存 3 段音频笔记、单条最长 10 分钟；订阅用户不限条数、单条
    最长 4 小时。超条数或单条超长都返回 402，客户端据此引导订阅。
    """
    if not file.content_type or not file.content_type.startswith("audio"):
        raise HTTPException(status_code=400, detail=t(language, "audio_required"))

    state = await get_state(db, user_id)
    if not state.can_create_voice_note:
        raise HTTPException(
            status_code=402,
            detail=t(language, "subscription_rate_limit", limit=FREE_VOICE_NOTE_LIMIT),
        )
    # 时长是客户端上报的，只用于额度判断和详情页展示。这里不截断：截断等于
    # 把超限的笔记放过来，用户随后会在录音界面上限之外看到一条可疑记录。
    if duration_seconds > state.voice_note_max_seconds:
        raise HTTPException(
            status_code=402,
            detail=t(
                language,
                "voice_note_too_long",
                limit=state.voice_note_max_seconds // 60,
            ),
        )

    file_path, file_size = await _spool_voice_upload(
        file,
        settings.MAX_AUDIO_UPLOAD_MB,
        language,
    )
    owns_file = True
    try:
        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=conversation_id or "",
            content_type="voice",
            content="",
            file_path=file_path,
            file_size=file_size,
            audio_duration_seconds=duration_seconds,
            filename=file.filename or "audio.wav",
            content_type_header=file.content_type,
            language=language,
        )
        if result.transcription_status == "pending":
            # 临时文件交给后台转写任务，由它收尾删除。
            background_tasks.add_task(
                transcribe_voice_record,
                record_id=result.record_id,
                user_id=user_id,
                file_path=file_path,
                filename=file.filename or "audio.wav",
                language=language,
                reason_mapper=asr_reason_code,
            )
            owns_file = False
        return _workflow_response(result, language)
    except HTTPException:
        raise
    except AuthenticationError as e:
        logger.exception("创建语音记录时模型服务认证失败")
        raise _model_auth_error(language) from e
    except (OpenAIError, ASRServiceError) as e:
        logger.exception("创建语音记录时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("创建语音记录失败")
        raise HTTPException(
            status_code=500, detail=t(language, "voice_record_failed", error=e)
        ) from e
    finally:
        if owns_file:
            await asyncio.to_thread(_remove_temp_file, file_path)


@router.post("/records/image", response_model=RecordResponse)
async def create_image_record(
    file: Annotated[UploadFile, File(...)],
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
    conversation_id: Annotated[str | None, Form()] = None,
):
    """上传图片记录。视觉识别失败时仍保留图片附件记录。"""
    if not file.content_type or not file.content_type.startswith("image"):
        raise HTTPException(status_code=400, detail=t(language, "image_required"))

    state = await get_state(db, user_id)
    if not state.can_create_image_note:
        raise HTTPException(
            status_code=402,
            detail=t(language, "subscription_rate_limit", limit=FREE_IMAGE_NOTE_LIMIT),
        )

    try:
        file_data = await _read_upload(file, settings.MAX_IMAGE_UPLOAD_MB, language)
        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=conversation_id or "",
            content_type="image",
            file_data=file_data,
            filename=file.filename or "image.jpg",
            content_type_header=file.content_type,
            language=language,
        )
        return _workflow_response(result, language)
    except HTTPException:
        raise
    except AuthenticationError as e:
        logger.exception("创建图片记录时模型服务认证失败")
        raise _model_auth_error(language) from e
    except OpenAIError as e:
        logger.exception("创建图片记录时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("创建图片记录失败")
        raise HTTPException(
            status_code=500, detail=t(language, "image_record_failed", error=e)
        ) from e


@router.post("/records/file", response_model=RecordResponse)
async def create_file_record(
    file: Annotated[UploadFile, File(...)],
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
    conversation_id: Annotated[str | None, Form()] = None,
):
    """上传普通附件记录。"""
    state = await get_state(db, user_id)
    if not state.can_create_file_note:
        raise HTTPException(
            status_code=402,
            detail=t(language, "subscription_rate_limit", limit=FREE_FILE_NOTE_LIMIT),
        )

    try:
        file_data = await _read_upload(file, settings.MAX_FILE_UPLOAD_MB, language)
        if not file_data:
            raise HTTPException(status_code=400, detail=t(language, "file_required"))

        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=conversation_id or "",
            content_type="file",
            file_data=file_data,
            filename=file.filename or "attachment",
            content_type_header=file.content_type or "application/octet-stream",
            language=language,
        )
        return _workflow_response(result, language)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("创建附件记录失败")
        raise HTTPException(
            status_code=500, detail=t(language, "file_record_failed", error=e)
        ) from e


@router.get("/records", response_model=RecordListResponse)
async def list_records(
    db: DbSession,
    user_id: CurrentUserId,
    skip: int = 0,
    limit: int = 50,
):
    """获取当前用户的记录列表。"""
    uid = uuid.UUID(user_id)
    count = (
        await db.execute(
            select(func.count()).select_from(Record).where(Record.user_id == uid)
        )
    ).scalar_one()
    result = await db.execute(
        select(Record)
        .where(Record.user_id == uid)
        .order_by(Record.created_at.desc())
        .offset(skip)
        .limit(min(limit, 100))
    )
    records = list(result.scalars().all())
    return RecordListResponse(
        records=[_record_response(record) for record in records],
        total=count,
    )


@router.get("/records/{record_id}", response_model=RecordResponse)
async def get_record(
    record_id: uuid.UUID,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """获取单条记录。"""
    result = await db.execute(
        select(Record).where(
            Record.id == record_id,
            Record.user_id == uuid.UUID(user_id),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=t(language, "record_not_found"))
    return _record_response(record, language=language)


@router.patch("/records/{record_id}", response_model=RecordResponse)
async def update_record(
    record_id: uuid.UUID,
    data: RecordUpdate,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """编辑记录内容并尽量刷新向量。"""
    result = await db.execute(
        select(Record).where(
            Record.id == record_id,
            Record.user_id == uuid.UUID(user_id),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=t(language, "record_not_found"))

    record.content = data.content.strip()
    if data.summary is not None and data.summary.strip():
        record.summary = data.summary.strip()
    elif record.content_type == "voice":
        try:
            record.summary = await LLMService().summarize_record(
                record.content,
                language,
            )
        except Exception:
            logger.warning("编辑语音后重新生成摘要失败: %s", record.id, exc_info=True)
            record.summary = (
                record.content[:100] + "..." if len(record.content) > 100 else None
            )
    else:
        record.summary = record.content[:100] + (
            "..." if len(record.content) > 100 else ""
        )
    # 统一按 UTC 存储，交给前端按本地时区展示。
    record.updated_at = datetime.now(UTC)

    try:
        embedded = await embed_text_chunks(EmbeddingService(), record.content)
        vector_db = get_vector_client()
        record.vector_id = await asyncio.to_thread(
            vector_db.replace_chunks,
            chunks=embedded,
            record_id=str(record.id),
            user_id=user_id,
        )
    except Exception:
        logger.warning("编辑记录后刷新向量失败: %s", record.id, exc_info=True)

    await db.flush()
    return _record_response(record, language=language)


@router.post(
    "/records/{record_id}/retry-transcription",
    response_model=RecordResponse,
)
async def retry_transcription(
    record_id: uuid.UUID,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
    background_tasks: BackgroundTasks,
):
    """Pro 用户手动重新提交失败语音的后台转写。"""
    if not await is_subscribed(db, user_id):
        raise HTTPException(
            status_code=402,
            detail=t(language, "subscription_required"),
        )

    result = await db.execute(
        select(Record)
        .where(
            Record.id == record_id,
            Record.user_id == uuid.UUID(user_id),
        )
        .with_for_update()
    )
    record = result.scalar_one_or_none()
    if record is None or record.content_type != "voice":
        raise HTTPException(
            status_code=404,
            detail=t(language, "record_not_found"),
        )
    if record.transcription_status == "pending":
        raise HTTPException(
            status_code=409,
            detail=t(language, "transcription_already_pending"),
        )
    if record.manual_transcription_count >= settings.MANUAL_TRANSCRIPTION_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=t(
                language,
                "manual_transcription_limit",
                limit=settings.MANUAL_TRANSCRIPTION_LIMIT,
            ),
        )
    if not record.file_url:
        raise HTTPException(
            status_code=404,
            detail=t(language, "audio_not_found"),
        )

    path = urlparse(record.file_url).path.lstrip("/")
    if path.startswith(f"{settings.API_PREFIX.lstrip('/')}/media/"):
        path = path[len(settings.API_PREFIX.lstrip("/")) + len("/media/") :]
    candidates = resolve_object_candidates(path)
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=t(language, "audio_not_found"),
        )
    minio = get_minio_client()

    def download_audio() -> str:
        """从对象存储流式落到临时文件，长录音不经过内存。

        历史数据 key 形态不统一，按候选顺序逐个尝试，第一个命中即停。
        """
        suffix = Path(record.file_name or "").suffix or ".audio"
        last_error: S3Error | None = None
        for candidate in candidates:
            fd, temp_path = tempfile.mkstemp(prefix="retry-", suffix=suffix)
            target = os.fdopen(fd, "wb")
            response = None
            ok = False
            try:
                response = minio.client.get_object(minio.bucket, candidate)
                for chunk in response.stream(1024 * 1024):
                    target.write(chunk)
                target.close()
                ok = True
                return temp_path
            except S3Error as error:
                # key 形态没对上，试下一个候选
                last_error = error
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
                if not ok:
                    if not target.closed:
                        target.close()
                    Path(temp_path).unlink(missing_ok=True)
        assert last_error is not None
        raise last_error

    try:
        file_path = await asyncio.to_thread(download_audio)
    except Exception as exc:
        logger.warning("手动转写读取录音失败: %s", record_id, exc_info=True)
        raise HTTPException(
            status_code=404,
            detail=t(language, "audio_not_found"),
        ) from exc

    try:
        record.transcription_status = "pending"
        record.transcription_error = None
        record.transcription_heartbeat_at = None
        record.manual_transcription_count += 1
        record.updated_at = datetime.now(UTC)
        await db.commit()
    except BaseException:
        await asyncio.to_thread(_remove_temp_file, file_path)
        raise

    background_tasks.add_task(
        transcribe_voice_record,
        record_id=str(record.id),
        user_id=user_id,
        file_path=file_path,
        filename=record.file_name or "audio.wav",
        language=language,
        reason_mapper=asr_reason_code,
    )
    return _record_response(record, language=language)


@router.delete("/records/{record_id}")
async def delete_record(
    record_id: uuid.UUID,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """删除记录。"""
    result = await db.execute(
        select(Record).where(
            Record.id == record_id,
            Record.user_id == uuid.UUID(user_id),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=t(language, "record_not_found"))

    if record.vector_id:
        try:
            vector_db = get_vector_client()
            await asyncio.to_thread(
                vector_db.delete_record, str(record.id), user_id
            )
        except Exception:
            logger.warning("删除记录向量失败: %s", record.id, exc_info=True)

    # 先清对象存储：删库之后就再也定位不到这些文件了。失败只记日志，不阻塞删除。
    minio = get_minio_client()
    await asyncio.to_thread(
        minio.delete_files_by_url, [record.image_url, record.file_url]
    )

    await db.delete(record)
    return {"message": "已删除"}
