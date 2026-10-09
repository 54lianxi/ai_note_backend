"""应用配置管理"""
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置"""
    
    # 应用基础配置
    APP_NAME: str = "AI-Record"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"

    # 客户端版本检测：App 启动时拉 /app/version 决定要不要提示更新。
    APP_LATEST_VERSION: str = "1.0.0"
    APP_MIN_VERSION: str = ""  # 填了比它低的版本会强制更新
    APP_UPDATE_URL: str = ""  # APK 或应用商店地址
    APP_UPDATE_NOTE: str = ""  # 更新说明
    
    # 数据库配置
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/ai_record",
        description="PostgreSQL 异步连接字符串"
    )
    
    # Redis 配置
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis 连接字符串"
    )
    
    # MinIO 配置
    MINIO_ENDPOINT: str = Field(default="localhost:9000")
    MINIO_ACCESS_KEY: str = Field(default="minioadmin")
    MINIO_SECRET_KEY: str = Field(default="minioadmin")
    MINIO_BUCKET: str = Field(default="records")
    MINIO_SECURE: bool = False
    
    # Qdrant 向量数据库配置
    # 填了 QDRANT_URL 就用远程（Qdrant Cloud），否则用下面的 host/port 连本地。
    QDRANT_URL: str = Field(default="")
    QDRANT_API_KEY: str = Field(default="")
    QDRANT_HOST: str = Field(default="localhost")
    QDRANT_PORT: int = Field(default=6333)
    QDRANT_COLLECTION: str = Field(default="records")
    
    # 大模型配置（对话、意图识别、图片理解）。
    # 默认走 Google Gemini 的 OpenAI 兼容层，换成别的厂商只改这三项。
    LLM_API_KEY: str = Field(default="")
    # 访问大模型服务走哪个代理。留空直连；国内开发环境填 http://127.0.0.1:7897 这类。
    # 部署到美东服务器后留空即可。
    AI_HTTP_PROXY: str = Field(default="")
    LLM_BASE_URL: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    # 默认用 flash-lite：Gemini 3 系关不掉思考链，flash 光思考就要十几秒，
    # 而这个应用的对话和摘要都很短，lite 的延迟只有一半左右。
    LLM_MODEL: str = Field(default="gemini-3.5-flash-lite")
    # 意图分类单独指定模型。留空复用 LLM_MODEL；
    # 把 LLM_MODEL 换成更强的模型后，可以把这项钉在 lite 上保住速度。
    LLM_CLASSIFIER_MODEL: str = Field(default="")
    # 图片理解模型。留空则复用 LLM_MODEL（Gemini 的 Flash 本身就是多模态）。
    VISION_MODEL: str = Field(default="")

    # 向量配置。
    # provider=gemini 走原生 embedContent（能指定维度，检索任务可区分文档/查询）；
    # provider=openai 走 OpenAI 兼容的 /embeddings。
    EMBEDDING_PROVIDER: str = Field(default="gemini")
    EMBEDDING_API_KEY: str = Field(default="")
    EMBEDDING_BASE_URL: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta"
    )
    EMBEDDING_MODEL: str = Field(default="gemini-embedding-2")
    EMBEDDING_DIM: int = Field(default=1536)

    # 语音转文字（录音文件）。
    # provider=gemini 走 Files API + /v1beta/interactions；
    # provider=openai 走 OpenAI 兼容的 /audio/transcriptions（Groq、OpenAI、硅基流动都适用）。
    ASR_PROVIDER: str = Field(default="gemini")
    ASR_API_KEY: str = Field(default="")
    ASR_BASE_URL: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta"
    )
    ASR_MODEL: str = Field(default="gemini-3.5-transcribe")
    # 转写语言提示。留空表示自动检测（Gemini 和 Whisper 都支持）。
    ASR_LANGUAGE: str = Field(default="")

    # 实时转写（录音过程中的字幕）。
    # provider=gemini 走 Live API 的 BidiGenerateContent；
    # provider=openai 走 OpenAI Realtime 协议。
    ASR_REALTIME_PROVIDER: str = Field(default="gemini")
    ASR_REALTIME_MODEL: str = Field(default="gemini-3.5-transcribe-live")
    ASR_REALTIME_URL: str = Field(
        default=(
            "wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        )
    )
    # 实时转写的语言提示，BCP-47 代码，留空表示自动检测。
    ASR_REALTIME_LANGUAGE: str = Field(default="")
    # 实时转写走哪个代理。留空表示直连（macOS 上 websockets 默认会读系统代理，
    # 系统里配了 SOCKS 代理时会直接报 python-socks 缺失）。
    ASR_REALTIME_PROXY: str = Field(default="")

    # Apple 登录配置
    APPLE_CLIENT_ID: str = Field(default="com.airecord.front")
    APPLE_JWKS_URL: str = Field(default="https://appleid.apple.com/auth/keys")

    # Google 登录配置。多个 Client ID 用逗号分隔。
    GOOGLE_CLIENT_IDS: str = Field(default="")
    GOOGLE_TOKEN_INFO_URL: str = Field(
        default="https://oauth2.googleapis.com/tokeninfo"
    )
    
    # JWT 认证配置
    SECRET_KEY: str = Field(default="your-secret-key-change-in-production")
    ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60 * 24)  # 24小时
    # 仅用于本地开发免登录，生产环境必须保持关闭。
    DEV_AUTH_BYPASS: bool = Field(default=False)

    @model_validator(mode="after")
    def _reuse_llm_key(self) -> "Settings":
        """向量和语音没单独配 key 时复用大模型的 key。

        现在三家能力都指向 Gemini，一把 key 就够；将来换厂商再单独填。
        """
        if not self.EMBEDDING_API_KEY:
            self.EMBEDDING_API_KEY = self.LLM_API_KEY
        if not self.ASR_API_KEY:
            self.ASR_API_KEY = self.LLM_API_KEY
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


# 全局配置实例
settings = Settings()
