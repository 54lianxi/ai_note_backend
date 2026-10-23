"""后端返回给用户的文案与 AI 回答语言。

语言来自请求的 Accept-Language，认不出来的语言一律回落到默认语言。
"""
from typing import Annotated

from fastapi import Depends, Header

Language = str

# 现有客户端没带 Accept-Language 时保持原行为。
DEFAULT_LANGUAGE: Language = "zh"

_SUPPORTED: tuple[str, ...] = (
    "en",
    "zh",
    "es",
    "fr",
    "de",
    "pt",
    "it",
    "ja",
    "ko",
    "ru",
    "ar",
    "hi",
    "id",
    "tr",
    "vi",
    "th",
)

_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Simplified Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "id": "Indonesian",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "th": "Thai",
}


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
            return base
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
        "record_saved_transcribing": "已保存，正在后台转写",
        "record_done": "记录成功",
        "record_done_no_vector": "记录成功，向量化暂不可用",
        "model_auth_failed": "AI 服务暂时不可用，请稍后再试。",
        "model_service_failed": "AI 服务开小差了，请稍后再试。",
        "record_failed": "记录失败: {error}",
        "voice_record_failed": "语音记录失败: {error}",
        "image_record_failed": "图片记录失败: {error}",
        "file_record_failed": "附件记录失败: {error}",
        "search_failed": "检索失败: {error}",
        "text_message_failed": "处理文本失败: {error}",
        "audio_required": "请上传音频文件",
        "image_required": "请上传图片",
        "file_required": "附件不能为空",
        "audio_not_found": "找不到这条记录的原始录音",
        "transcription_already_pending": "这条录音正在转写，请稍后再试",
        "manual_transcription_limit": "这条录音最多手动转写 {limit} 次",
        "upload_too_large": "文件不能超过 {limit} MB，请选择更小的文件",
        "record_not_found": "记录不存在",
        "conversation_not_found": "对话不存在",
        "account_deleted": "账户已注销",
        "account_not_found": "账户不存在",
        "image_record": "图片记录",
        "image_text": "图片文字：\n{text}",
        "image_description": "图片描述：\n{text}",
        "subscription_required": "该功能需要订阅后使用",
        "subscription_rate_limit": "该类笔记已达免费上限（{limit} 项），订阅后不限量",
        "subscription_product_unknown": "订阅商品不匹配",
        "subscription_receipt_required": "缺少订阅凭证",
        "subscription_receipt_invalid": "订阅凭证无效或已过期",
        "subscription_not_active": "订阅未生效或已过期",
        "subscription_verify_unavailable": "订阅校验暂时不可用，请稍后重试",
        "subscription_token_in_use": "该购买凭证已绑定其他账号",
        "subscription_platform_unsupported": "该平台暂不支持订阅校验",
    },
    "en": {
        "no_results": "Sorry, I couldn't find any notes related to your query.",
        "query_done": "Search complete",
        "record_saved": "Saved",
        "record_saved_no_vector": "Saved, but vector indexing is temporarily unavailable",
        "record_saved_transcribing": "Saved. Transcribing in the background",
        "record_done": "Saved",
        "record_done_no_vector": "Saved, but vector indexing is temporarily unavailable",
        "model_auth_failed": "The AI service is temporarily unavailable. Please try again later.",
        "model_service_failed": "The AI service hit a snag. Please try again later.",
        "record_failed": "Failed to save the record: {error}",
        "voice_record_failed": "Failed to save the voice note: {error}",
        "image_record_failed": "Failed to save the image note: {error}",
        "file_record_failed": "Failed to save the attachment: {error}",
        "search_failed": "Search failed: {error}",
        "text_message_failed": "Failed to process the text: {error}",
        "audio_required": "Please upload an audio file",
        "image_required": "Please upload an image",
        "file_required": "The attachment can't be empty",
        "audio_not_found": "The original audio for this note was not found",
        "transcription_already_pending": "This recording is already being transcribed",
        "manual_transcription_limit": "This recording can be manually transcribed up to {limit} times",
        "upload_too_large": "Files can't exceed {limit} MB. Please choose a smaller file",
        "record_not_found": "Record not found",
        "conversation_not_found": "Conversation not found",
        "account_deleted": "Your account has been deleted",
        "account_not_found": "Account not found",
        "image_record": "Image note",
        "image_text": "Text in image:\n{text}",
        "image_description": "Image description:\n{text}",
        "subscription_required": "This feature requires a subscription",
        "subscription_rate_limit": "You have reached the free limit of {limit} items in this category. Subscribe for unlimited notes",
        "subscription_product_unknown": "Subscription product mismatch",
        "subscription_receipt_required": "Missing subscription receipt",
        "subscription_receipt_invalid": "The subscription receipt is invalid or expired",
        "subscription_not_active": "The subscription is not active or has expired",
        "subscription_verify_unavailable": "Subscription verification is temporarily unavailable. Please try again later",
        "subscription_token_in_use": "This purchase is already linked to another account",
        "subscription_platform_unsupported": "Subscription verification is not supported on this platform yet",
    },
}


def t(language: str | None, key: str, **kwargs: object) -> str:
    """取当前语言的文案，缺翻译时回落默认语言。"""
    lang = normalize_language(language)
    template = (
        _MESSAGES.get(lang, {}).get(key)
        or _MESSAGES["en"].get(key)
        or _MESSAGES[DEFAULT_LANGUAGE][key]
    )
    return template.format(**kwargs) if kwargs else template


_SEARCH_ANSWER_SYSTEM = {
    "zh": (
        "你是一个智能记录助手。根据用户的查询和检索到的相关记录，"
        "用自然语言组织一个清晰、准确的回答。"
        "如果检索结果中没有与查询直接相关的信息，请如实告知用户。"
        "回答要简洁，重点突出。用中文回答。"
        "如果用户请求违法、仇恨、性暴力、伤害他人或其他明显有害内容，"
        "不要提供帮助，只简短说明无法协助。\n\n"
        "你必须以严格的 JSON 格式返回，包含以下两个字段：\n"
        '- "answer": 给用户的自然语言回答文本\n'
        '- "relevant_indices": 与用户查询直接相关的记录编号列表（整数数组）。'
        "如果某条记录的内容与查询无直接关系，不要包含其编号。"
        "如果没有相关记录，返回空数组 []。"
    ),
    "en": (
        "You are a smart note assistant. Using the user's query and the retrieved "
        "notes, write a clear and accurate answer in natural language. "
        "If the notes contain nothing directly relevant, say so honestly. "
        "Keep the answer concise and to the point. Answer in English. "
        "If the user asks for illegal, hateful, sexually violent, harmful or "
        "otherwise clearly dangerous content, do not help; briefly say you "
        "cannot assist.\n\n"
        "You MUST return a strict JSON object with exactly two fields:\n"
        '- "answer": the natural language answer text\n'
        '- "relevant_indices": a list of integer indices for records that are '
        "directly relevant to the user's query. "
        "Do not include indices of records that are not directly relevant. "
        "Return an empty array [] if no records are relevant."
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
        "ask": "请根据以上信息回答用户的问题。注意：记录编号从 1 开始，请在 relevant_indices 中使用这些编号。",
    },
    "en": {
        "record": "Note",
        "score": "relevance",
        "summary": "Summary",
        "content": "Content",
        "query": "User query",
        "records": "Retrieved notes",
        "ask": "Answer the user's question based on the information above. Note: record indices start from 1; use these numbers in relevant_indices.",
    },
}


def search_answer_prompts(
    language: str | None,
    query: str,
    results: list[dict],
) -> tuple[str, str]:
    """返回 (system_prompt, user_prompt)，用用户的语言提问和作答。"""
    lang = normalize_language(language)
    labels = _CONTEXT_LABELS.get(lang, _CONTEXT_LABELS["en"])

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
    system_prompt = _SEARCH_ANSWER_SYSTEM.get(lang)
    if system_prompt is None:
        language_name = _LANGUAGE_NAMES.get(lang, lang)
        system_prompt = (
            _SEARCH_ANSWER_SYSTEM["en"].replace(
                "Answer in English.",
                f"Answer in {language_name} ({lang}).",
            )
        )
    return system_prompt, prompt


_VISION_SYSTEM = {
    "zh": (
        "你是一个图片分析助手。请分析用户上传的图片，提取以下信息：\n"
        "1. extracted_text: 图片中的文字内容（OCR），按图片里的排版保留换行；"
        "不同区域或段落之间用空行隔开；没有文字则为 null\n"
        "2. description: 图片内容的详细描述，分成 1~3 个自然段，段与段之间用空行隔开\n"
        "3. summary: 一句话摘要\n\n"
        "请以 JSON 格式返回结果，用中文填写内容。"
    ),
    "en": (
        "You are an image analysis assistant. Analyze the uploaded image and extract:\n"
        "1. extracted_text: any text in the image (OCR), keeping the original line "
        "breaks and separating blocks/paragraphs with a blank line; null if none\n"
        "2. description: a detailed description in 1-3 paragraphs, separated by blank lines\n"
        "3. summary: a one-sentence summary\n\n"
        "Return the result as JSON, written in English."
    ),
}


def vision_system_prompt(language: str | None) -> str:
    lang = normalize_language(language)
    prompt = _VISION_SYSTEM.get(lang)
    if prompt is not None:
        return prompt
    language_name = _LANGUAGE_NAMES.get(lang, lang)
    return _VISION_SYSTEM["en"].replace(
        "written in English.",
        f"written in {language_name} ({lang}).",
    )
