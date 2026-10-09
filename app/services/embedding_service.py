"""Embedding 向量化服务"""
import asyncio

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core.http import build_async_client

# 检索场景下文档和查询要用不同的任务类型，向量空间才对得上。
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


class EmbeddingService:
    """文本向量化服务。

    provider=gemini 走原生 embedContent（能精确控制维度）；
    其他厂商走 OpenAI 兼容的 /embeddings。
    """

    def __init__(self):
        self.provider = settings.EMBEDDING_PROVIDER
        self.model = settings.EMBEDDING_MODEL
        self.dim = settings.EMBEDDING_DIM
        self.base_url = settings.EMBEDDING_BASE_URL.rstrip("/")
        self.api_key = settings.EMBEDDING_API_KEY
        self.client = AsyncOpenAI(
            api_key=self.api_key or "not-configured",
            base_url=self.base_url,
            http_client=build_async_client(),
        )

    async def embed(
        self,
        text: str,
        max_retries: int = 5,
        task_type: str = TASK_DOCUMENT,
    ) -> list[float]:
        """
        将文本转为向量（含指数退避重试）
        :param text: 输入文本
        :param max_retries: 最大重试次数
        :param task_type: 检索任务类型，查询侧传 TASK_QUERY
        :return: 向量列表
        """
        if self.provider == "gemini":
            return await self._embed_gemini(text, task_type, max_retries)

        for attempt in range(max_retries):
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=text,
                    dimensions=self.dim,
                )
                return response.data[0].embedding
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    await asyncio.sleep(wait)
                    continue
                raise

    async def embed_batch(
        self,
        texts: list[str],
        max_retries: int = 5,
        task_type: str = TASK_DOCUMENT,
    ) -> list[list[float]]:
        """
        批量向量化（含指数退避重试）
        :param texts: 输入文本列表
        :param max_retries: 最大重试次数
        :param task_type: 检索任务类型
        :return: 向量列表
        """
        if self.provider == "gemini":
            return await self._embed_gemini_batch(texts, task_type, max_retries)

        for attempt in range(max_retries):
            try:
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=texts,
                    dimensions=self.dim,
                )
                return [item.embedding for item in response.data]
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    await asyncio.sleep(wait)
                    continue
                raise

    async def _embed_gemini(
        self,
        text: str,
        task_type: str,
        max_retries: int,
    ) -> list[float]:
        payload = {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self.dim,
        }
        body = await self._post_gemini(
            f"/models/{self.model}:embedContent", payload, max_retries
        )
        return body["embedding"]["values"]

    async def _embed_gemini_batch(
        self,
        texts: list[str],
        task_type: str,
        max_retries: int,
    ) -> list[list[float]]:
        payload = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": self.dim,
                }
                for text in texts
            ]
        }
        body = await self._post_gemini(
            f"/models/{self.model}:batchEmbedContents", payload, max_retries
        )
        return [item["values"] for item in body["embeddings"]]

    async def _post_gemini(
        self,
        path: str,
        payload: dict,
        max_retries: int,
    ) -> dict:
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                async with build_async_client(60) as client:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        headers=headers,
                        json=payload,
                    )
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code < 400:
                    return response.json()
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = RuntimeError(
                        f"向量化失败 {response.status_code}: {response.text[:200]}"
                    )
                else:
                    raise RuntimeError(
                        f"向量化失败 {response.status_code}: {response.text[:200]}"
                    )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))

        raise RuntimeError(f"向量化请求失败: {last_error}")
