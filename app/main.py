"""FastAPI 应用入口"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 导入所有 ORM 模型，确保 create_all 能发现全部表
import app.models
from app.api.record import run_stalled_transcription_reaper
from app.api.router import api_router
from app.config import settings
from app.infrastructure.database import close_db, init_db
from app.infrastructure.minio_client import get_minio_client
from app.infrastructure.vector_db import get_vector_client

# 静态页面目录（隐私政策等公开法律文本）。
STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def _warm_storage_clients() -> None:
    """构造 Qdrant / MinIO 客户端单例，同步阻塞，由调用方放线程里执行。"""
    get_vector_client()
    get_minio_client()


def _static_page(filename: str) -> FileResponse:
    """返回 static 目录下的 HTML 页面。"""
    return FileResponse(STATIC_DIR / filename, media_type="text/html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    await init_db()
    # Qdrant/MinIO 客户端首次构造要发建桶、建集合等远程请求，放在线程里预先做完，
    # 免得重启后第一个用户请求替整个进程付这段阻塞时间。失败不致命，留到用时重试。
    try:
        await asyncio.to_thread(_warm_storage_clients)
    except Exception:
        logger.warning("外部存储预热失败，将在首次请求时重试", exc_info=True)
    # 后台转写跑在 API 进程内，进程重启就丢了；用定时巡检把失联任务收尾，
    # 免得用户永远看到「正在转写」。
    reaper = asyncio.create_task(run_stalled_transcription_reaper())
    try:
        yield
    finally:
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        # 关闭时
        await close_db()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI 多模态智能记录 App 后端服务",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.CORS_ORIGINS.split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(api_router, prefix=settings.API_PREFIX)

# 本地文件夹存储的公开静态路由：仅附件记录（records/file）走这里，
# 语音/图片仍走 MinIO。目录在挂载时确保存在。
_STATIC_FILES_DIR = Path(settings.STATIC_FILES_DIR)
_STATIC_FILES_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    settings.STATIC_FILES_URL_PREFIX,
    StaticFiles(directory=_STATIC_FILES_DIR),
    name="static_files",
)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": settings.APP_VERSION}


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/home", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
async def home_page() -> FileResponse:
    """应用首页，供 Google OAuth 验证等场景展示产品用途。"""
    return _static_page("index.html")


@app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/privacy.html", methods=["GET", "HEAD"], include_in_schema=False)
async def privacy_policy() -> FileResponse:
    """隐私政策页面，供 App 内跳转和应用商店审核使用。"""
    return _static_page("privacy.html")


@app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/terms.html", methods=["GET", "HEAD"], include_in_schema=False)
async def terms_of_service() -> FileResponse:
    """服务条款页面。"""
    return _static_page("terms.html")


@app.api_route("/account-deletion", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route(
    "/account-deletion.html", methods=["GET", "HEAD"], include_in_schema=False
)
@app.api_route("/delete-account", methods=["GET", "HEAD"], include_in_schema=False)
async def account_deletion() -> FileResponse:
    """账号注销申请页面，供应用内跳转和应用商店「账号删除」链接使用。"""
    return _static_page("account-deletion.html")


@app.api_route("/app-ads.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def app_ads() -> FileResponse:
    """AdMob 授权卖方文件，必须放在站点根路径。"""
    return FileResponse(
        STATIC_DIR / "app-ads.txt",
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
