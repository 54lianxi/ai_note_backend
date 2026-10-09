"""LangGraph 记录工作流 - 处理多模态输入并存储"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.core.i18n import t
from app.infrastructure.database import async_session_factory
from app.infrastructure.minio_client import get_minio_client
from app.infrastructure.vector_db import get_vector_client
from app.models.record import Record
from app.services.asr_service import ASRService
from app.services.embedding_service import EmbeddingService
from app.services.vision_service import VisionService

logger = logging.getLogger(__name__)


@dataclass
class RecordState:
    """记录工作流状态"""
    # 输入
    user_id: str
    content_type: Literal["text", "voice", "image", "file"]
    content: str = ""  # 文本内容或处理后的文本
    language: str | None = None  # 模型生成内容（图片描述等）的语言
    file_data: bytes | None = None  # 原始文件数据
    filename: str = ""
    content_type_header: str = ""  # HTTP Content-Type
    
    # 中间状态
    transcribed_text: str = ""  # ASR 转写结果
    image_key_info: dict = field(default_factory=dict)  # 视觉模型提取的信息
    image_url: str = ""  # MinIO 图片 URL
    file_url: str = ""  # MinIO 通用附件 URL
    embedding_vector: list[float] = field(default_factory=list)
    summary: str = ""
    
    # 输出
    record_id: str = ""
    created_at: str = ""  # 记录入库时间（UTC，ISO 字符串）
    updated_at: str = ""  # 记录更新时间（UTC，ISO 字符串）
    error: str = ""


# ========== 节点函数 ==========

async def process_text_node(state: RecordState) -> dict:
    """处理文本输入：直接使用文本"""
    state.content = state.content.strip()
    state.summary = state.content[:100] + "..." if len(state.content) > 100 else state.content
    return {"content": state.content, "summary": state.summary}


async def process_voice_node(state: RecordState) -> dict:
    """处理语音输入：音频存对象存储 + ASR 转写"""
    # 1. 保存原始音频，记录详情页可以回放（失败不影响记录本身）。
    if state.file_data:
        try:
            minio = get_minio_client()
            state.file_url = minio.upload_file(
                state.file_data,
                state.content_type_header or "audio/wav",
                filename=state.filename or None,
            )
        except Exception:
            logger.exception("语音上传对象存储失败，继续保存录音记录元数据")

    if state.content.strip():
        text = state.content.strip()
        summary = text[:100] + "..." if len(text) > 100 else text
    else:
        asr = ASRService()
        result = await asr.transcribe_with_summary(state.file_data, state.filename)
        text = result["text"]
        summary = result["summary"]

    state.content = text
    state.summary = summary
    state.transcribed_text = text
    return {
        "content": text,
        "summary": summary,
        "transcribed_text": text,
        "file_url": state.file_url,
    }


async def process_image_node(state: RecordState) -> dict:
    """处理图片输入：视觉模型提取 + MinIO 上传"""
    # 1. 上传图片到 MinIO
    try:
        minio = get_minio_client()
        state.image_url = minio.upload_file(
            state.file_data,
            state.content_type_header,
            filename=state.filename,
        )
        state.file_url = state.image_url
    except Exception:
        logger.exception("图片上传对象存储失败，继续保存图片记录元数据")
    
    # 2. 视觉模型提取关键信息
    try:
        vision = VisionService()
        key_info = await vision.extract_key_info(
            state.file_data,
            state.content_type_header,
            language=state.language,
        )
    except Exception:
        logger.exception("图片视觉识别失败，使用图片文件名作为记录内容")
        key_info = {}
    state.image_key_info = key_info
    
    # 3. 将提取的信息作为文本内容
    parts = []
    if key_info.get("extracted_text"):
        parts.append(t(state.language, "image_text", text=key_info["extracted_text"]))
    if key_info.get("description"):
        parts.append(
            t(state.language, "image_description", text=key_info["description"])
        )
    fallback = t(state.language, "image_record")
    state.content = "\n".join(parts) if parts else fallback
    state.summary = key_info.get("summary") or fallback
    
    return {
        "image_url": state.image_url,
        "file_url": state.file_url,
        "image_key_info": key_info,
        "content": state.content,
        "summary": state.summary,
    }


async def process_file_node(state: RecordState) -> dict:
    """处理普通附件：上传文件并建立可检索的元数据记录"""
    try:
        minio = get_minio_client()
        state.file_url = minio.upload_file(
            state.file_data,
            state.content_type_header or "application/octet-stream",
            filename=state.filename,
        )
    except Exception:
        logger.exception("附件上传对象存储失败，继续保存附件元数据")

    filename = state.filename.strip() or "未命名附件"
    state.content = f"附件：{filename}"
    state.summary = filename
    return {
        "file_url": state.file_url,
        "content": state.content,
        "summary": state.summary,
    }


async def embed_node(state: RecordState) -> dict:
    """文本向量化"""
    embedding = EmbeddingService()
    try:
        vector = await embedding.embed(state.content)
    except Exception as e:  # noqa: BLE001
        state.error = f"向量化失败: {e!s}"
        logger.warning("向量化失败，记录将不写入向量数据库: %s", e)
        return {"embedding_vector": [], "error": state.error}

    state.embedding_vector = vector
    return {"embedding_vector": vector}


async def store_node(state: RecordState) -> dict:
    """存储到向量数据库和 PostgreSQL"""
    record_id = str(uuid.uuid4())
    
    # 1. 写入向量数据库
    vector_id = None
    if state.embedding_vector:
        try:
            vector_db = get_vector_client()
            vector_id = vector_db.upsert(
                vector=state.embedding_vector,
                payload={
                    "record_id": record_id,
                    "user_id": state.user_id,
                    "content": state.content,
                    "summary": state.summary,
                    "content_type": state.content_type,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                point_id=record_id,
            )
        except Exception as e:
            state.error = f"向量数据库写入失败: {e!s}"
            logger.exception("写入向量数据库失败，继续保存记录")
    
    # 2. 写入 PostgreSQL
    import json
    async with async_session_factory() as session:
        record = Record(
            id=uuid.UUID(record_id),
            user_id=uuid.UUID(state.user_id),
            content=state.content,
            summary=state.summary,
            content_type=state.content_type,
            image_url=state.image_url or None,
            image_key_info=json.dumps(state.image_key_info, ensure_ascii=False) if state.image_key_info else None,
            vector_id=vector_id,
            file_url=state.file_url or None,
            file_name=state.filename or None,
            file_content_type=state.content_type_header or None,
            file_size=len(state.file_data) if state.file_data else None,
        )
        session.add(record)
        
        # 笔记只写 records 表，不再往 conversations/messages 里插消息，
        # 否则笔记内容会出现在 AI 聊天的对话记录里。
        # 取数据库生成的时间（UTC），避免响应里用服务器本地时间伪造。
        await session.flush()
        await session.refresh(record)
        created_at = record.created_at
        updated_at = record.updated_at
        await session.commit()
    
    state.record_id = record_id
    state.created_at = created_at.isoformat() if created_at else ""
    state.updated_at = updated_at.isoformat() if updated_at else ""
    return {
        "record_id": record_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


# ========== 路由函数 ==========

def route_by_content_type(state: RecordState) -> str:
    """根据输入类型路由到不同处理节点"""
    return state.content_type


# ========== 构建工作流 ==========

def build_record_workflow() -> StateGraph:
    """构建记录工作流图"""
    workflow = StateGraph(RecordState)
    
    # 添加处理节点
    workflow.add_node("process_text", process_text_node)
    workflow.add_node("process_voice", process_voice_node)
    workflow.add_node("process_image", process_image_node)
    workflow.add_node("process_file", process_file_node)
    workflow.add_node("embed", embed_node)
    workflow.add_node("store", store_node)
    
    # 条件路由：根据输入类型分发
    workflow.add_conditional_edges(
        START,
        route_by_content_type,
        {
            "text": "process_text",
            "voice": "process_voice",
            "image": "process_image",
            "file": "process_file",
        },
    )
    
    # 所有处理完成后 → 向量化 → 存储
    workflow.add_edge("process_text", "embed")
    workflow.add_edge("process_voice", "embed")
    workflow.add_edge("process_image", "embed")
    workflow.add_edge("process_file", "embed")
    workflow.add_edge("embed", "store")
    workflow.add_edge("store", END)
    
    return workflow.compile()


# 全局工作流实例
record_workflow = build_record_workflow()


async def execute_record_workflow(
    user_id: str,
    content_type: str,
    conversation_id: str = "",  # 笔记已与对话解耦，仅为兼容旧客户端保留
    content: str = "",
    file_data: bytes | None = None,
    filename: str = "",
    content_type_header: str = "",
    language: str | None = None,
) -> RecordState:
    """执行记录工作流"""
    initial_state = RecordState(
        user_id=user_id,
        content_type=content_type,
        content=content,
        file_data=file_data,
        filename=filename,
        content_type_header=content_type_header,
        language=language,
    )
    
    result = await record_workflow.ainvoke(initial_state)
    if isinstance(result, RecordState):
        return result
    return RecordState(**result)
