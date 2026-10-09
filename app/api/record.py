"""记录上传和记录管理接口"""
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from openai import AuthenticationError, OpenAIError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId
from app.core.timeutil import ensure_utc, parse_utc
from app.infrastructure.database import get_db
from app.infrastructure.vector_db import get_vector_client
from app.models.record import Record
from app.schemas.record import (
    RecordListResponse,
    RecordResponse,
    RecordUpdate,
    TextRecordCreate,
    VoiceTranscriptionResponse,
)
from app.services.asr_service import ASRService, ASRServiceError
from app.services.embedding_service import EmbeddingService
from app.workflows.record_workflow import execute_record_workflow

router = APIRouter(tags=["记录管理"])
logger = logging.getLogger(__name__)
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _model_auth_error(language: str | None) -> HTTPException:
    return HTTPException(status_code=502, detail=t(language, "model_auth_failed"))


def _model_service_error(error: Exception, language: str | None) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=t(language, "model_service_failed", error=error),
    )


def _record_response(record: Record, message: str | None = None,
                     language: str | None = None) -> RecordResponse:
    message = message or t(language, "record_saved")
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
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _workflow_response(result, language: str | None = None) -> RecordResponse:
    created_at = parse_utc(result.created_at) or datetime.now(UTC)
    return RecordResponse(
        record_id=uuid.UUID(result.record_id),
        message=t(
            language,
            "record_saved" if not result.error else "record_saved_no_vector",
        ),
        content=result.content,
        summary=result.summary,
        content_type=result.content_type,
        image_url=result.image_url or None,
        image_key_info=str(result.image_key_info) if result.image_key_info else None,
        file_url=result.file_url or None,
        file_name=result.filename or None,
        file_content_type=result.content_type_header or None,
        file_size=len(result.file_data) if result.file_data else None,
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
    user_id: CurrentUserId,
    language: LanguageDep,
    conversation_id: Annotated[str | None, Form()] = None,
    transcribed_text: Annotated[str | None, Form()] = None,
):
    """上传语音记录。传入转写文本时跳过重复 ASR。"""
    if not file.content_type or not file.content_type.startswith("audio"):
        raise HTTPException(status_code=400, detail=t(language, "audio_required"))

    try:
        file_data = await file.read()
        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=conversation_id or "",
            content_type="voice",
            content=transcribed_text or "",
            file_data=file_data,
            filename=file.filename or "audio.wav",
            content_type_header=file.content_type,
            language=language,
        )
        return _workflow_response(result, language)
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


@router.post("/records/voice/transcribe", response_model=VoiceTranscriptionResponse)
async def transcribe_voice(
    file: Annotated[UploadFile, File(...)],
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """只转写语音，允许客户端编辑后再正式保存。"""
    if not file.content_type or not file.content_type.startswith("audio"):
        raise HTTPException(status_code=400, detail=t(language, "audio_required"))

    try:
        file_data = await file.read()
        result = await ASRService().transcribe_with_summary(
            file_data,
            file.filename or "audio.wav",
        )
        return VoiceTranscriptionResponse(
            text=result["text"],
            summary=result["summary"],
        )
    except AuthenticationError as e:
        logger.exception("语音转写时模型服务认证失败")
        raise _model_auth_error(language) from e
    except (OpenAIError, ASRServiceError) as e:
        logger.exception("语音转写时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("语音转写失败")
        raise HTTPException(
            status_code=500, detail=t(language, "transcribe_failed", error=e)
        ) from e


@router.post("/records/image", response_model=RecordResponse)
async def create_image_record(
    file: Annotated[UploadFile, File(...)],
    user_id: CurrentUserId,
    language: LanguageDep,
    conversation_id: Annotated[str | None, Form()] = None,
):
    """上传图片记录。视觉识别失败时仍保留图片附件记录。"""
    if not file.content_type or not file.content_type.startswith("image"):
        raise HTTPException(status_code=400, detail=t(language, "image_required"))

    try:
        file_data = await file.read()
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
    user_id: CurrentUserId,
    language: LanguageDep,
    conversation_id: Annotated[str | None, Form()] = None,
):
    """上传普通附件记录。"""
    try:
        file_data = await file.read()
        if not file_data:
            raise HTTPException(
                status_code=400, detail=t(language, "file_required")
            )

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
    return RecordListResponse(
        records=[_record_response(record) for record in result.scalars().all()],
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
        raise HTTPException(
            status_code=404, detail=t(language, "record_not_found")
        )
    return _record_response(record)


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
        raise HTTPException(
            status_code=404, detail=t(language, "record_not_found")
        )

    record.content = data.content.strip()
    record.summary = (
        data.summary.strip()
        if data.summary is not None and data.summary.strip()
        else record.content[:100] + ("..." if len(record.content) > 100 else "")
    )
    # 统一按 UTC 存储，交给前端按本地时区展示。
    record.updated_at = datetime.now(UTC)

    try:
        vector = await EmbeddingService().embed(record.content)
        vector_id = str(record.id)
        get_vector_client().upsert(
            vector=vector,
            payload={
                "record_id": str(record.id),
                "user_id": user_id,
                "content": record.content,
                "summary": record.summary,
                "content_type": record.content_type,
                "created_at": (
                    ensure_utc(record.created_at).isoformat()
                    if record.created_at
                    else None
                ),
            },
            point_id=vector_id,
        )
        record.vector_id = vector_id
    except Exception:
        logger.warning("编辑记录后刷新向量失败: %s", record.id, exc_info=True)

    await db.flush()
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
        raise HTTPException(
            status_code=404, detail=t(language, "record_not_found")
        )

    if record.vector_id:
        try:
            get_vector_client().delete(record.vector_id)
        except Exception:
            logger.warning("删除记录向量失败: %s", record.id, exc_info=True)

    await db.delete(record)
    return {"message": "已删除"}
