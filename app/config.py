"""应用配置管理"""

import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_APP_ENV = os.getenv("APP_ENV", "")
_ENV_FILENAME = os.getenv("ENV_FILE", ".env.prod" if _APP_ENV == "production" else ".env")
_ENV_FILE = _ENV_FILENAME
if not Path(_ENV_FILE).is_absolute():
    _ENV_FILE = str(_BACKEND_DIR / _ENV_FILE)


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # 应用基础配置
    APP_NAME: str = "AI-Record"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"
    # 移动端不依赖 CORS；公开网页只需要正式站点来源。
    CORS_ORIGINS: str = "https://kuliqi.us"

    # 客户端版本检测：App 启动时拉 /app/version 决定要不要提示更新。
    APP_LATEST_VERSION: str = "1.0.0"
    APP_MIN_VERSION: str = ""  # 填了比它低的版本会强制更新
    APP_UPDATE_URL: str = ""  # APK 或应用商店地址
    APP_UPDATE_NOTE: str = ""  # 更新说明
    # 首次请求某语言时生成的 UI 语言包缓存目录。
    LANGUAGE_PACK_CACHE_DIR: str = "/tmp/ai-record-language-packs"

    # 上传大小硬限制。前端会在请求前先校验；这里防止绕过客户端直传超大资源。
    # 64MB 可容纳约 2 小时 19 分钟的 64kbps 录音；nginx 需同步放宽到 70MB。
    MAX_IMAGE_UPLOAD_MB: int = Field(default=8, gt=0)
    MAX_AUDIO_UPLOAD_MB: int = Field(default=64, gt=0)
    MAX_FILE_UPLOAD_MB: int = Field(default=45, gt=0)
    # 单份文档最多抽取/索引的字符数，避免超大文件产生过多向量。
    MAX_DOCUMENT_TEXT_CHARS: int = Field(default=300_000, gt=0)
    
    # 数据库配置
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/ai_record",
        description="PostgreSQL 异步连接字符串"
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

    # 向量检索的最低相似度。gemini-embedding-2 的分数整体偏高、区分度小，
    # 真正相关的记录也常常只有 0.45~0.55，阈值 0.5 会把图片笔记这类
    # 长文本记录直接过滤掉。
    SEARCH_SCORE_THRESHOLD: float = Field(default=0.3)

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
    # 临时性 429/5xx/网络错误的重试次数与基础退避时间。
    ASR_MAX_RETRIES: int = Field(default=3, ge=0, le=5)
    ASR_RETRY_BASE_SECONDS: float = Field(default=2.0, ge=0, le=30)
    # 每条语音记录允许用户主动重新转写的总次数。
    MANUAL_TRANSCRIPTION_LIMIT: int = Field(default=3, ge=1, le=20)

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

    # 应用商店审核账号：审核员在登录页连点副标题 5 次后输入访问码。
    # 访问码只放在后端环境变量和 Play Console 的审核说明里，不写进 App。
    REVIEW_LOGIN_ENABLED: bool = Field(default=True)
    REVIEW_ACCESS_CODE: str = Field(default="")

    # 订阅（应用内购买）。App Store Connect 与 Google Play Console 使用相同 ID。
    SUBSCRIPTION_WEEKLY_PRODUCT_ID: str = Field(default="note_sub_week_0.99")
    SUBSCRIPTION_MONTHLY_PRODUCT_ID: str = Field(default="note_sub_month_2.99")
    # 免费用户的分类额度。录音与上传音频都记为 voice。
    SUBSCRIPTION_FREE_VOICE_LIMIT: int = Field(default=3, gt=0)
    SUBSCRIPTION_FREE_IMAGE_LIMIT: int = Field(default=3, gt=0)
    SUBSCRIPTION_FREE_FILE_LIMIT: int = Field(default=1, gt=0)
    # 仅本地开发免校验时使用，按多少天授予订阅。
    SUBSCRIPTION_FALLBACK_DAYS: int = Field(default=31)
    # 是否启用商店服务端校验。生产环境必须保持 true；关闭后接口会拒绝
    # 所有订阅上报，只有 DEBUG + SUBSCRIPTION_DEV_ALLOW_UNVERIFIED 才放行。
    SUBSCRIPTION_VERIFY_WITH_STORE: bool = Field(default=True)
    # 本地开发用 SKIP_IAP 调试时，需要同时打开 DEBUG 和这一项。
    # 生产环境即使误开，也会因为 DEBUG=false 而不生效。
    SUBSCRIPTION_DEV_ALLOW_UNVERIFIED: bool = Field(default=False)
    # 每多久重新向商店确认一次订阅状态。业务接口只读本地缓存，
    # 不受 Google API 延迟影响。
    SUBSCRIPTION_CACHE_TTL_SECONDS: int = Field(default=3600)

    # Google Play 服务端校验。
    # 服务账号需要开启 Android Publisher API，并在 Play Console 里授予
    # 「查看财务数据」和「管理订单和订阅」权限。
    GOOGLE_PLAY_PACKAGE_NAME: str = Field(default="com.lx.note")
    GOOGLE_PLAY_SERVICE_ACCOUNT_FILE: str = Field(default="")
    # 也可以直接把服务账号 JSON 塞进环境变量（适合容器 Secret）。
    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON: str = Field(default="")
    GOOGLE_PLAY_API_BASE: str = Field(
        default="https://androidpublisher.googleapis.com/androidpublisher/v3"
    )
    GOOGLE_OAUTH_TOKEN_URL: str = Field(
        default="https://oauth2.googleapis.com/token"
    )

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

# 全局配置实例
settings = Settings()
