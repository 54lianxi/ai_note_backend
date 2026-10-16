"""对话管理接口"""
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from openai import AuthenticationError, OpenAIError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId
from app.core.timeutil import parse_utc
from app.infrastructure.database import get_db
from app.models.conversation import Conversation
from app.models.conversation import Message
from app.schemas.chat import (
    ConversationCreate,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    MessageResponse,
    TextMessageRequest,
    TextMessageResponse,
)
from app.schemas.record import RecordResponse
from app.schemas.search import SearchResultItem
from app.services.llm_service import LLMService
from app.models.record import Record
from app.workflows.record_workflow import execute_record_workflow
from app.workflows.search_workflow import execute_search_workflow
from app.api.record import _record_response

router = APIRouter(tags=["对话管理"])
logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _model_auth_error(language: str | None) -> HTTPException:
    return HTTPException(status_code=502, detail=t(language, "model_auth_failed"))


def _model_service_error(error: OpenAIError, language: str | None) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=t(language, "model_service_failed", error=error),
    )


def _search_items(results: list[dict]) -> list[SearchResultItem]:
    items = []
    for r in results:
        items.append(SearchResultItem(
            record_id=uuid.UUID(r["record_id"]),
            content=r["content"],
            summary=r.get("summary"),
            score=r["score"],
            content_type=r["content_type"],
            image_url=r.get("image_url"),
            created_at=parse_utc(r.get("created_at")) or datetime.now(UTC),
        ))
    return items


def _related_record_ids(message: Message) -> list[uuid.UUID]:
    """消息里存的关联记录 ID。JSONB 里存的是字符串，脏数据直接跳过。"""
    ids: list[uuid.UUID] = []
    for value in message.related_record_ids or []:
        try:
            ids.append(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            logger.warning("消息 %s 的关联记录 ID 无效：%r", message.id, value)
    return ids


def _message_response(
    message: Message,
    related: dict[uuid.UUID, RecordResponse],
) -> MessageResponse:
    """消息 + 关联笔记，聊天里据此在回答下方渲染笔记链接。"""
    ids = _related_record_ids(message)
    return MessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
        content_type=message.content_type,
        record_id=message.record_id,
        related_record_ids=ids,
        related_records=[related[rid] for rid in ids if rid in related],
        created_at=message.created_at,
    )


@router.post("/messages/text", response_model=TextMessageResponse)
async def handle_text_message(
    data: TextMessageRequest,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """统一文本输入：先由大模型识别意图，再决定记录或检索"""
    try:
        intent = await LLMService().classify_text_intent(data.content)

        if intent == "search":
            result = await execute_search_workflow(
                user_id=user_id,
                conversation_id=str(data.conversation_id),
                query=data.content,
                top_k=5,
                language=language,
            )
            items = _search_items(result.results)
            return TextMessageResponse(
                intent="search",
                message=t(language, "query_done"),
                answer=result.answer,
                results=items,
            )

        result = await execute_record_workflow(
            user_id=user_id,
            conversation_id=str(data.conversation_id),
            content_type="text",
            content=data.content,
            language=language,
        )
        record = RecordResponse(
            record_id=uuid.UUID(result.record_id),
            message=t(
                language,
                "record_done" if not result.error else "record_done_no_vector",
            ),
            summary=result.summary,
            content_type="text",
            created_at=parse_utc(result.created_at) or datetime.now(UTC),
            updated_at=parse_utc(result.updated_at),
        )
        return TextMessageResponse(
            intent="record",
            message=record.message,
            record=record,
        )
    except AuthenticationError as e:
        logger.exception("处理文本消息时模型服务认证失败")
        raise _model_auth_error(language) from e
    except OpenAIError as e:
        logger.exception("处理文本消息时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("处理文本消息失败")
        raise HTTPException(
            status_code=500, detail=t(language, "text_message_failed", error=e)
        )


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    data: ConversationCreate,
    db: DbSession,
    user_id: CurrentUserId,
):
    """创建新对话"""
    conv = Conversation(
        user_id=uuid.UUID(user_id),
        title=data.title,
    )
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return conv


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    db: DbSession,
    user_id: CurrentUserId,
    skip: int = 0,
    limit: int = 20,
):
    """获取对话列表"""
    uid = uuid.UUID(user_id)
    
    # 查询总数
    count_stmt = select(func.count()).select_from(Conversation).where(Conversation.user_id == uid)
    total = (await db.execute(count_stmt)).scalar()
    
    # 查询列表
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == uid)
        .order_by(Conversation.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    conversations = result.scalars().all()
    
    return ConversationListResponse(conversations=conversations, total=total)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """获取对话详情（含消息列表）"""
    stmt = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .where(Conversation.id == conversation_id, Conversation.user_id == uuid.UUID(user_id))
    )
    result = await db.execute(stmt)
    conv = result.scalar_one_or_none()
    
    if not conv:
        raise HTTPException(
            status_code=404, detail=t(language, "conversation_not_found")
        )

    # 检索类回答关联的笔记一次性查出来，附在消息上，聊天里就能在回答
    # 下方直接给出可点的笔记链接；没有关联笔记的消息保持空列表。
    related_ids = {
        rid for msg in conv.messages for rid in _related_record_ids(msg)
    }
    related: dict[uuid.UUID, RecordResponse] = {}
    if related_ids:
        rows = await db.execute(
            select(Record).where(
                Record.id.in_(related_ids),
                Record.user_id == uuid.UUID(user_id),
            )
        )
        related = {
            record.id: _record_response(record, language=language)
            for record in rows.scalars()
        }

    return ConversationDetailResponse(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        messages=[_message_response(msg, related) for msg in conv.messages],
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    db: DbSession,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """删除对话"""
    stmt = select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.user_id == uuid.UUID(user_id),
    )
    result = await db.execute(stmt)
    conv = result.scalar_one_or_none()
    
    if not conv:
        raise HTTPException(
            status_code=404, detail=t(language, "conversation_not_found")
        )
    
    await db.delete(conv)
    return {"message": "删除成功"}
