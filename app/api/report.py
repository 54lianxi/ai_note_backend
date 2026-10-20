"""AI 回复举报接口。"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CurrentUserId
from app.infrastructure.database import get_db
from app.models.conversation import Conversation, Message
from app.models.report import AIReport
from app.schemas.report import AIReportCreate

router = APIRouter(tags=["AI 举报"])
DbSession = Annotated[AsyncSession, Depends(get_db)]


@router.post("/reports/ai-content", status_code=201)
async def report_ai_content(
    data: AIReportCreate,
    db: DbSession,
    user_id: CurrentUserId,
):
    """保存用户对 AI 回复的举报；消息必须属于当前用户。"""
    uid = uuid.UUID(user_id)
    message = (
        await db.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Message.id == data.message_id,
                Message.conversation_id == data.conversation_id,
                Message.role == "assistant",
                Conversation.user_id == uid,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise HTTPException(status_code=404, detail="AI 回复不存在")

    db.add(
        AIReport(
            user_id=uid,
            conversation_id=data.conversation_id,
            message_id=data.message_id,
            reason=data.reason,
            details=(data.details or "").strip() or None,
        )
    )
    await db.flush()
    return {"message": "举报已提交"}
