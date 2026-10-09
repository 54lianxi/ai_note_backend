"""LangGraph 检索工作流 - 语义检索并生成回答"""
import uuid
from dataclasses import dataclass, field

from langgraph.graph import END, START, StateGraph

from app.core.i18n import t
from app.core.timeutil import ensure_utc
from app.infrastructure.database import async_session_factory
from app.infrastructure.vector_db import get_vector_client
from app.models.conversation import Message
from app.models.record import Record
from app.services.embedding_service import TASK_QUERY, EmbeddingService
from app.services.llm_service import LLMService


@dataclass
class SearchState:
    """检索工作流状态"""
    # 输入
    user_id: str
    conversation_id: str
    query: str = ""
    top_k: int = 5
    language: str | None = None  # 回答语言，来自请求的 Accept-Language
    
    # 中间状态
    query_vector: list[float] = field(default_factory=list)
    search_results: list[dict] = field(default_factory=list)  # 向量检索结果
    full_records: list[dict] = field(default_factory=list)    # 完整记录
    
    # 输出
    answer: str = ""
    results: list[dict] = field(default_factory=list)
    error: str = ""


# ========== 节点函数 ==========

async def embed_query_node(state: SearchState) -> dict:
    """将查询文本向量化"""
    embedding = EmbeddingService()
    vector = await embedding.embed(state.query, task_type=TASK_QUERY)
    state.query_vector = vector
    return {"query_vector": vector}


async def vector_search_node(state: SearchState) -> dict:
    """在向量数据库中检索"""
    vector_db = get_vector_client()
    results = vector_db.search(
        vector=state.query_vector,
        top_k=state.top_k,
        user_id=state.user_id,
    )
    state.search_results = results
    return {"search_results": results}


async def load_records_node(state: SearchState) -> dict:
    """从 PostgreSQL 加载完整记录"""
    if not state.search_results:
        return {"full_records": []}
    
    record_ids = [r["payload"].get("record_id") for r in state.search_results if r["payload"].get("record_id")]
    
    async with async_session_factory() as session:
        from sqlalchemy import select
        stmt = select(Record).where(Record.id.in_([uuid.UUID(rid) for rid in record_ids]))
        result = await session.execute(stmt)
        records = result.scalars().all()
    
    # 构建完整记录列表（合并向量分数）
    score_map = {r["payload"].get("record_id"): r["score"] for r in state.search_results}
    
    full_records = []
    for record in records:
        full_records.append({
            "record_id": str(record.id),
            "content": record.content,
            "summary": record.summary,
            "content_type": record.content_type,
            "image_url": record.image_url,
            "score": score_map.get(str(record.id), 0),
            "created_at": (
                ensure_utc(record.created_at).isoformat()
                if record.created_at
                else None
            ),
        })
    
    # 按分数排序
    full_records.sort(key=lambda x: x["score"], reverse=True)
    state.full_records = full_records
    
    return {"full_records": full_records}


async def generate_answer_node(state: SearchState) -> dict:
    """使用 LLM 生成自然语言回答"""
    if not state.full_records:
        state.answer = t(state.language, "no_results")
        return {"answer": state.answer, "results": []}
    
    llm = LLMService()
    answer = await llm.generate_search_answer(
        state.query,
        state.full_records,
        language=state.language,
    )
    state.answer = answer
    state.results = state.full_records
    
    # 写入对话消息（用户提问）
    async with async_session_factory() as session:
        user_msg = Message(
            conversation_id=uuid.UUID(state.conversation_id),
            role="user",
            content=state.query,
            content_type="text",
        )
        session.add(user_msg)
        
        # 写入 AI 回答
        assistant_msg = Message(
            conversation_id=uuid.UUID(state.conversation_id),
            role="assistant",
            content=answer,
            content_type="result",
            related_record_ids=[r["record_id"] for r in state.results],
        )
        session.add(assistant_msg)
        
        await session.commit()
    
    return {"answer": answer, "results": state.results}


# ========== 构建工作流 ==========

def build_search_workflow() -> StateGraph:
    """构建检索工作流图"""
    workflow = StateGraph(SearchState)
    
    # 添加节点
    workflow.add_node("embed_query", embed_query_node)
    workflow.add_node("vector_search", vector_search_node)
    workflow.add_node("load_records", load_records_node)
    workflow.add_node("generate_answer", generate_answer_node)
    
    # 连接边
    workflow.add_edge(START, "embed_query")
    workflow.add_edge("embed_query", "vector_search")
    workflow.add_edge("vector_search", "load_records")
    workflow.add_edge("load_records", "generate_answer")
    workflow.add_edge("generate_answer", END)
    
    return workflow.compile()


# 全局工作流实例
search_workflow = build_search_workflow()


async def execute_search_workflow(
    user_id: str,
    conversation_id: str,
    query: str,
    top_k: int = 5,
    language: str | None = None,
) -> SearchState:
    """执行检索工作流"""
    initial_state = SearchState(
        user_id=user_id,
        conversation_id=conversation_id,
        query=query,
        top_k=top_k,
        language=language,
    )
    
    result = await search_workflow.ainvoke(initial_state)
    if isinstance(result, SearchState):
        return result
    return SearchState(**result)
