"""LLM 服务 - 大语言模型调用"""
import json
import re
from typing import Literal

from openai import AsyncOpenAI, BadRequestError

from app.config import settings
from app.core.http import build_async_client
from app.core.i18n import search_answer_prompts

TextIntent = Literal["record", "search"]


class LLMService:
    """LLM 服务（基于 OpenAI API）"""
    
    def __init__(self):
        self.client = AsyncOpenAI(
            # 没配 key 时先给个占位值，让错误在请求时以 401 的形式暴露出来，
            # 而不是在构造客户端时抛一个看不懂的 OpenAIError。
            api_key=settings.LLM_API_KEY or "not-configured",
            base_url=settings.LLM_BASE_URL,
            http_client=build_async_client(),
        )
        self.model = settings.LLM_MODEL
    
    async def generate(self, prompt: str, system_prompt: str | None = None) -> str:
        """
        生成文本
        :param prompt: 用户输入
        :param system_prompt: 系统提示词
        :return: 生成的文本
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7,
            max_tokens=1000,
        )
        
        return response.choices[0].message.content

    async def summarize_record(
        self,
        text: str,
        language: str | None = None,
    ) -> str:
        """把口述内容压缩成便于回看的事实摘要，而不是摘抄原文。"""
        language_hint = (
            f"使用语言代码 {language} 输出。"
            if language
            else "使用与原文相同的语言输出。"
        )
        system_prompt = (
            "你负责总结个人语音笔记。只依据原文，不补充事实。"
            "用一到两句话讲清核心信息；有日期、人物、金额、数量、决定或待办时保留。"
            "不要逐句复述，不要写“这段内容主要讲了”等套话，不要在摘要开头重复原文。"
            f"{language_hint}"
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text[:12000]},
            ],
            temperature=0.2,
            max_tokens=180,
        )
        summary = (response.choices[0].message.content or "").strip()
        if not summary:
            raise ValueError("摘要模型返回了空内容")
        return summary[:500]

    async def translate_language_pack(
        self,
        source: dict[str, str],
        language: str,
    ) -> dict[str, str]:
        """翻译完整 UI 语言包，并严格保留键名和插值占位符。"""
        system_prompt = (
            f"你是移动应用本地化译者。把 JSON 中所有界面文案翻译成语言代码 {language}。"
            "必须返回相同数量的键，键名保持英文不变；{name} 形式占位符必须原样保留。"
            "只返回一个合法 JSON 对象，不要 Markdown 代码块，不要解释。"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(source, ensure_ascii=False),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 8000,
        }
        try:
            response = await self.client.chat.completions.create(
                **payload,
                response_format={"type": "json_object"},
            )
        except BadRequestError:
            response = await self.client.chat.completions.create(**payload)

        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        translated = json.loads(raw)
        if not isinstance(translated, dict):
            raise ValueError("语言包翻译结果不是 JSON 对象")

        result: dict[str, str] = {}
        for key, source_text in source.items():
            value = translated.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"语言包缺少文案: {key}")
            if set(re.findall(r"\{\w+\}", source_text)) != set(
                re.findall(r"\{\w+\}", value)
            ):
                raise ValueError(f"语言包占位符不一致: {key}")
            result[key] = value.strip()
        return result

    async def classify_text_intent(self, text: str) -> TextIntent:
        """
        判断用户文本意图：record 表示存新内容，search 表示查已有内容。
        """
        system_prompt = (
            "你是智能记录应用的意图分类器，只判断用户这句话应该“存储”还是“查询”。"
            "不要回答用户问题，不要抽取内容，只输出 JSON。\n"
            "分类规则：\n"
            "- record: 用户在陈述一个新事实、进度、想法、计划、状态，希望系统记住。\n"
            "- search: 用户在询问、查找、回忆之前记录过的内容。\n"
            "示例：\n"
            "海贼王看到了600集 -> record\n"
            "海贼王看到了第几集 -> search\n"
            "我今天做了肩背训练 -> record\n"
            "我今天练了什么 -> search\n"
            "记一下我明天早上跑步 -> record\n"
            "查一下我明天要做什么 -> search\n"
            "只输出：{\"intent\":\"record\"} 或 {\"intent\":\"search\"}"
        )

        payload = {
            "model": settings.LLM_CLASSIFIER_MODEL or self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            # Gemini 3 系关不掉思考链，预算给小了会把 JSON 截断成半截，
            # 所以这里留够空间，靠提示词约束输出长度。
            "max_tokens": 256,
        }
        try:
            response = await self.client.chat.completions.create(
                **payload,
                response_format={"type": "json_object"},
            )
        except BadRequestError:
            # 部分厂商的兼容层不认 response_format，提示词里已经要求只输出 JSON。
            response = await self.client.chat.completions.create(**payload)

        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return self._classify_text_intent_fallback(text)

        intent = data.get("intent")
        if intent in ("record", "search"):
            return intent
        return self._classify_text_intent_fallback(text)

    def _classify_text_intent_fallback(self, text: str) -> TextIntent:
        """模型输出格式异常时的保守兜底，默认偏向记录。"""
        compact = "".join(text.split())
        if any(word in compact for word in ("查一下", "查询", "搜索", "找一下", "帮我查")):
            return "search"
        if "?" in compact or "？" in compact:
            return "search"
        if any(word in compact for word in ("第几", "多少", "什么时候", "哪天", "哪里", "哪个", "什么")):
            return "search"
        return "record"
    
    async def generate_search_answer(
        self,
        query: str,
        results: list[dict],
        language: str | None = None,
    ) -> tuple[str, list[int]]:
        """
        根据检索结果生成自然语言回答，并返回相关记录编号。
        :param query: 用户查询
        :param results: 检索结果列表
        :param language: 回答语言（zh / en）
        :return: (自然语言回答, 相关记录的 1-based 编号列表)
        """
        system_prompt, prompt = search_answer_prompts(language, query, results)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 1000,
        }
        try:
            response = await self.client.chat.completions.create(
                **payload,
                response_format={"type": "json_object"},
            )
        except BadRequestError:
            response = await self.client.chat.completions.create(**payload)

        raw = response.choices[0].message.content or ""
        try:
            data = json.loads(raw)
            answer = data.get("answer", raw)
            relevant_indices = data.get("relevant_indices", [])
            if not isinstance(relevant_indices, list):
                relevant_indices = []
            relevant_indices = [
                i for i in relevant_indices
                if isinstance(i, int) and 1 <= i <= len(results)
            ]
            return answer, relevant_indices
        except (json.JSONDecodeError, AttributeError):
            return raw, list(range(1, len(results) + 1))
