"""LLM 服务 - 大语言模型调用"""
import json
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
    ) -> str:
        """
        根据检索结果生成自然语言回答
        :param query: 用户查询
        :param results: 检索结果列表
        :param language: 回答语言（zh / en）
        :return: 自然语言回答
        """
        system_prompt, prompt = search_answer_prompts(language, query, results)
        return await self.generate(prompt, system_prompt)
