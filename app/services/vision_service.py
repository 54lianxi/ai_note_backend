"""视觉模型服务 - 图片关键信息提取"""
import base64
from openai import AsyncOpenAI
from app.config import settings
from app.core.i18n import vision_system_prompt
from app.core.http import build_async_client


class VisionService:
    """视觉模型服务（基于 GPT-4o 多模态能力）"""
    
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY or "not-configured",
            base_url=settings.LLM_BASE_URL,
            http_client=build_async_client(),
        )
        self.model = settings.VISION_MODEL or settings.LLM_MODEL
    
    async def extract_key_info(
        self,
        image_data: bytes,
        content_type: str = "image/jpeg",
        language: str | None = None,
    ) -> dict:
        """
        提取图片关键信息
        :param image_data: 图片字节数据
        :param content_type: MIME 类型
        :param language: 提取结果的语言（zh / en）
        :return: {"extracted_text": "OCR文本", "description": "图片描述", "summary": "摘要"}
        """
        # 将图片编码为 base64
        image_base64 = base64.b64encode(image_data).decode("utf-8")
        data_url = f"data:{content_type};base64,{image_base64}"
        
        # 调用视觉模型
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": vision_system_prompt(language),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        }
                    ],
                },
            ],
            max_tokens=1000,
            response_format={"type": "json_object"},
        )
        
        result = response.choices[0].message.content
        
        # 解析 JSON 结果
        import json
        try:
            info = json.loads(result)
        except json.JSONDecodeError:
            info = {"extracted_text": None, "description": result, "summary": result[:100]}
        
        return {
            "extracted_text": info.get("extracted_text"),
            "description": info.get("description"),
            "summary": info.get("summary"),
        }
