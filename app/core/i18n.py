"""后端返回给用户的文案与 AI 回答语言。

语言来自请求的 Accept-Language，认不出来的语言一律回落到默认语言。
"""
from typing import Annotated, Literal

from fastapi import Depends, Header

Language = Literal["zh", "en"]

# 现有客户端没带 Accept-Language 时保持原行为。
DEFAULT_LANGUAGE: Language = "zh"

_SUPPORTED: tuple[str, ...] = ("zh", "en")


def normalize_language(raw: str | None) -> Language:
    """把 Accept-Language（如 "en-US,en;q=0.9"）归一化到支持的语言。"""
    if not raw:
        return DEFAULT_LANGUAGE
    for part in raw.split(","):
        code = part.split(";")[0].strip().lower()
        if not code:
            continue
        base = code.split("-")[0]
        if base in _SUPPORTED:
            return base  # type: ignore[return-value]
    return DEFAULT_LANGUAGE


def current_language(
    accept_language: Annotated[str | None, Header()] = None,
) -> Language:
    """FastAPI 依赖：从请求头解析用户语言。"""
    return normalize_language(accept_language)


LanguageDep = Annotated[Language, Depends(current_language)]


# 固定文案
_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "no_results": "抱歉，没有找到与您查询相关的记录。",
        "query_done": "查询完成",
        "record_saved": "已保存",
        "record_saved_no_vector": "已保存，向量化暂不可用",
        "record_done": "记录成功",
        "record_done_no_vector": "记录成功，向量化暂不可用",
        "model_auth_failed": "AI 服务暂时不可用，请稍后再试。",
        "model_service_failed": "AI 服务开小差了，请稍后再试。",
        "record_failed": "记录失败: {error}",
        "voice_record_failed": "语音记录失败: {error}",
        "transcribe_failed": "语音转写失败: {error}",
        "image_record_failed": "图片记录失败: {error}",
        "file_record_failed": "附件记录失败: {error}",
        "search_failed": "检索失败: {error}",
        "text_message_failed": "处理文本失败: {error}",
        "audio_required": "请上传音频文件",
        "image_required": "请上传图片",
        "file_required": "附件不能为空",
        "record_not_found": "记录不存在",
        "conversation_not_found": "对话不存在",
        "image_record": "图片记录",
        "image_text": "图片文字: {text}",
        "image_description": "图片描述: {text}",
    },
    "en": {
        "no_results": "Sorry, I couldn't find any notes related to your query.",
        "query_done": "Search complete",
        "record_saved": "Saved",
        "record_saved_no_vector": "Saved, but vector indexing is temporarily unavailable",
        "record_done": "Saved",
        "record_done_no_vector": "Saved, but vector indexing is temporarily unavailable",
        "model_auth_failed": "The AI service is temporarily unavailable. Please try again later.",
        "model_service_failed": "The AI service hit a snag. Please try again later.",
        "record_failed": "Failed to save the record: {error}",
        "voice_record_failed": "Failed to save the voice note: {error}",
        "transcribe_failed": "Transcription failed: {error}",
        "image_record_failed": "Failed to save the image note: {error}",
        "file_record_failed": "Failed to save the attachment: {error}",
        "search_failed": "Search failed: {error}",
        "text_message_failed": "Failed to process the text: {error}",
        "audio_required": "Please upload an audio file",
        "image_required": "Please upload an image",
        "file_required": "The attachment can't be empty",
        "record_not_found": "Record not found",
        "conversation_not_found": "Conversation not found",
        "image_record": "Image note",
        "image_text": "Text in image: {text}",
        "image_description": "Image description: {text}",
    },
}


def t(language: str | None, key: str, **kwargs: object) -> str:
    """取当前语言的文案，缺翻译时回落默认语言。"""
    lang = normalize_language(language)
    template = _MESSAGES.get(lang, {}).get(key) or _MESSAGES[DEFAULT_LANGUAGE][key]
    return template.format(**kwargs) if kwargs else template


_SEARCH_ANSWER_SYSTEM = {
    "zh": (
        "你是一个智能记录助手。根据用户的查询和检索到的相关记录，"
        "用自然语言组织一个清晰、准确的回答。"
        "如果检索结果中没有相关信息，请如实告知用户。"
        "回答要简洁，重点突出。用中文回答。"
    ),
    "en": (
        "You are a smart note assistant. Using the user's query and the retrieved "
        "notes, write a clear and accurate answer in natural language. "
        "If the notes contain nothing relevant, say so honestly. "
        "Keep the answer concise and to the point. Answer in English."
    ),
}

_CONTEXT_LABELS = {
    "zh": {
        "record": "记录",
        "score": "相关度",
        "summary": "摘要",
        "content": "内容",
        "query": "用户查询",
        "records": "检索到的相关记录",
        "ask": "请根据以上信息回答用户的问题。",
    },
    "en": {
        "record": "Note",
        "score": "relevance",
        "summary": "Summary",
        "content": "Content",
        "query": "User query",
        "records": "Retrieved notes",
        "ask": "Answer the user's question based on the information above.",
    },
}


def search_answer_prompts(
    language: str | None,
    query: str,
    results: list[dict],
) -> tuple[str, str]:
    """返回 (system_prompt, user_prompt)，用用户的语言提问和作答。"""
    lang = normalize_language(language)
    labels = _CONTEXT_LABELS[lang]

    parts = []
    for index, item in enumerate(results, 1):
        score = item.get("score", 0)
        text = f"[{labels['record']}{index}] ({labels['score']}: {score:.2f})\n"
        if item.get("summary"):
            text += f"{labels['summary']}: {item['summary']}\n"
        text += f"{labels['content']}: {item.get('content', '')}\n"
        parts.append(text)

    prompt = (
        f"{labels['query']}: {query}\n\n"
        f"{labels['records']}:\n{''.join(parts)}\n\n"
        f"{labels['ask']}"
    )
    return _SEARCH_ANSWER_SYSTEM[lang], prompt


_VISION_SYSTEM = {
    "zh": (
        "你是一个图片分析助手。请分析用户上传的图片，提取以下信息：\n"
        "1. extracted_text: 图片中的文字内容（OCR），如果没有则为 null\n"
        "2. description: 图片内容的详细描述\n"
        "3. summary: 一句话摘要\n\n"
        "请以 JSON 格式返回结果，用中文填写内容。"
    ),
    "en": (
        "You are an image analysis assistant. Analyze the uploaded image and extract:\n"
        "1. extracted_text: any text in the image (OCR), or null if there is none\n"
        "2. description: a detailed description of the image\n"
        "3. summary: a one-sentence summary\n\n"
        "Return the result as JSON, written in English."
    ),
}


def vision_system_prompt(language: str | None) -> str:
    return _VISION_SYSTEM[normalize_language(language)]
