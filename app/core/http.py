"""统一的 HTTP 客户端工厂。

本地开发在国内直连不了 Google，需要挂代理；服务器在美东时留空直连即可。
所有访问大模型的请求都从这里拿客户端，免得每个服务各配一次代理。
"""
import httpx

from app.config import settings

# 转写要上传音频、模型要思考，给宽松一点。
DEFAULT_TIMEOUT = 120.0


def build_async_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """按配置造一个 httpx 异步客户端，未配代理时就是直连。"""
    return httpx.AsyncClient(
        timeout=timeout,
        proxy=settings.AI_HTTP_PROXY or None,
    )
