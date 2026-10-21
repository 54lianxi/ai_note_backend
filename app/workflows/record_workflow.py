"""LangGraph 记录工作流 - 处理多模态输入并存储"""
import asyncio
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
from app.services.document_service import extract_document_text
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.services.text_index import EMBED_BATCH_SIZE, split_text_chunks
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
    embedding_chunks: list[dict] = field(default_factory=list)
    summary: str = ""
    # 语音转写状态：done 已完成（或不需要转写）、pending 保存后由后台任务转写。
    transcription_status: str = "done"
    
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
    """处理语音输入：音频存对象存储。

    客户端已经带转写文本（实时转写）时直接用它的结果；否则保存请求立即返回，
    等后台任务再做 ASR，避免把转写耗时算进「保存笔记」。
    """
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
        try:
            summary = await LLMService().summarize_record(text, state.language)
        except Exception:
            logger.warning("实时转写语音摘要生成失败", exc_info=True)
            summary = f"{text[:100]}..." if len(text) > 100 else ""
        status = "done"
    else:
        text = ""
        summary = ""
        status = "pending"

    state.content = text
    state.summary = summary
    state.transcribed_text = text
    state.transcription_status = status
    return {
        "content": text,
        "summary": summary,
        "transcribed_text": text,
        "transcription_status": status,
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
    # 文字和描述各成一段，中间空一行，详情页和检索都更好读。
    state.content = "\n\n".join(parts) if parts else fallback
    state.summary = key_info.get("summary") or fallback
    
    return {
        "image_url": state.image_url,
        "file_url": state.file_url,
        "image_key_info": key_info,
        "content": state.content,
        "summary": state.summary,
    }


async def process_file_node(state: RecordState) -> dict:
    """上传附件并抽取正文，正文供详情展示和 AI 检索使用。"""
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
    try:
        extracted = await asyncio.to_thread(
            extract_document_text,
            state.file_data or b"",
            filename,
            state.content_type_header,
        )
    except Exception:
        logger.warning("文档正文抽取失败: %s", filename, exc_info=True)
        extracted = ""

    if extracted:
        state.content = f"文件名：{filename}\n\n{extracted}"
        state.summary = (
            extracted[:100] + "..." if len(extracted) > 100 else extracted
        )
    else:
        state.content = f"附件：{filename}"
        state.summary = filename
    return {
        "file_url": state.file_url,
        "content": state.content,
        "summary": state.summary,
    }


async def embed_text_chunks(
    embedding: EmbeddingService,
    text: str,
) -> list[dict]:
    chunks = split_text_chunks(text)
    vectors: list[list[float]] = []
    for index in range(0, len(chunks), EMBED_BATCH_SIZE):
        vectors.extend(
            await embedding.embed_batch(chunks[index : index + EMBED_BATCH_SIZE])
        )
    return [
        {"text": chunk, "vector": vector}
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]


async def embed_node(state: RecordState) -> dict:
    """文本向量化。

    语音记录在后台转写完成前 content 为空，此时跳过向量化，等转写完成后再补。
    """
    chunks = split_text_chunks(state.content)
    if not chunks:
        state.embedding_vector = []
        state.embedding_chunks = []
        return {"embedding_vector": [], "embedding_chunks": []}

    embedding = EmbeddingService()
    try:
        embedded = await embed_text_chunks(embedding, state.content)
    except Exception as e:  # noqa: BLE001
        state.error = f"向量化失败: {e!s}"
        logger.warning("向量化失败，记录将不写入向量数据库: %s", e)
        return {
            "embedding_vector": [],
            "embedding_chunks": [],
            "error": state.error,
        }

    state.embedding_chunks = embedded
    state.embedding_vector = embedded[0]["vector"] if embedded else []
    return {
        "embedding_vector": state.embedding_vector,
        "embedding_chunks": state.embedding_chunks,
    }


async def store_node(state: RecordState) -> dict:
    """存储到向量数据库和 PostgreSQL"""
    record_id = str(uuid.uuid4())
    
    # 1. 写入向量数据库
    vector_id = None
    if state.embedding_chunks:
        try:
            vector_db = get_vector_client()
            vector_id = vector_db.upsert_chunks(
                chunks=state.embedding_chunks,
                record_id=record_id,
                user_id=state.user_id,
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
            transcription_status=state.transcription_status,
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


async def transcribe_voice_record(
    record_id: str,
    user_id: str,
    file_data: bytes,
    filename: str,
    language: str | None = None,
) -> None:
    """保存语音记录后的后台转写：ASR → 向量化 → 回写记录。

    由保存接口用 FastAPI BackgroundTasks 调度，所以保存请求本身不等转写。
    转写失败只把状态标记为 failed，记录和音频都保留，用户可回放音频。
    """
    status = "done"
    text = ""
    summary: str | None = None
    try:
        result = await ASRService().transcribe_with_summary(
            file_data,
            filename,
            language=language,
        )
        text = (result.get("text") or "").strip()
        summary = result.get("summary") or None
    except Exception:
        status = "failed"
        logger.exception("语音记录后台转写失败，record_id=%s", record_id)

    vector_id = None
    if text:
        try:
            embedded = await embed_text_chunks(EmbeddingService(), text)
            vector_db = get_vector_client()
            vector_db.delete_record(record_id, user_id)
            vector_id = vector_db.upsert_chunks(
                chunks=embedded,
                record_id=record_id,
                user_id=user_id,
            )
        except Exception:
            logger.warning("语音记录后台向量化失败，record_id=%s", record_id, exc_info=True)

    async with async_session_factory() as session:
        record = await session.get(Record, uuid.UUID(record_id))
        if record is None:
            logger.warning("后台转写找不到记录，record_id=%s", record_id)
            return
        record.content = text
        record.summary = summary
        record.transcription_status = status
        if vector_id:
            record.vector_id = vector_id
        # 转写完成时间就是记录更新时间，前端据此判断内容是否已刷新。
        record.updated_at = datetime.now(UTC)
        await session.commit()
