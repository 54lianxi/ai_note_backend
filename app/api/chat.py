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
from app.schemas.chat import (
    ConversationCreate,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    TextMessageRequest,
    TextMessageResponse,
)
from app.schemas.record import RecordResponse
from app.schemas.search import SearchResultItem
from app.services.llm_service import LLMService
from app.workflows.record_workflow import execute_record_workflow
from app.workflows.search_workflow import execute_search_workflow

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
    
    return conv


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
