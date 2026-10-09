"""智能检索接口"""
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from openai import AuthenticationError, OpenAIError

from app.core.i18n import LanguageDep, t
from app.core.security import CurrentUserId
from app.core.timeutil import parse_utc
from app.schemas.search import SearchRequest, SearchResponse, SearchResultItem
from app.workflows.search_workflow import execute_search_workflow

router = APIRouter(tags=["智能检索"])
logger = logging.getLogger(__name__)

def _model_auth_error(language: str | None) -> HTTPException:
    return HTTPException(status_code=502, detail=t(language, "model_auth_failed"))


def _model_service_error(error: OpenAIError, language: str | None) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=t(language, "model_service_failed", error=error),
    )


@router.post("/search", response_model=SearchResponse)
async def search_records(
    data: SearchRequest,
    user_id: CurrentUserId,
    language: LanguageDep,
):
    """语义检索记录"""
    try:
        result = await execute_search_workflow(
            user_id=user_id,
            conversation_id=str(data.conversation_id),
            query=data.query,
            top_k=data.top_k,
            language=language,
        )
        
        # 构建响应
        items = []
        for r in result.results:
            items.append(SearchResultItem(
                record_id=uuid.UUID(r["record_id"]),
                content=r["content"],
                summary=r.get("summary"),
                score=r["score"],
                content_type=r["content_type"],
                image_url=r.get("image_url"),
                created_at=parse_utc(r.get("created_at")) or datetime.now(UTC),
            ))
        
        return SearchResponse(
            answer=result.answer,
            results=items,
        )
    except AuthenticationError as e:
        logger.exception("检索记录时模型服务认证失败")
        raise _model_auth_error(language) from e
    except OpenAIError as e:
        logger.exception("检索记录时模型服务调用失败")
        raise _model_service_error(e, language) from e
    except Exception as e:
        logger.exception("检索记录失败")
        raise HTTPException(
            status_code=500, detail=t(language, "search_failed", error=e)
        )
