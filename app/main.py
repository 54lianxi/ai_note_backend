"""FastAPI 应用入口"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.config import settings
from app.api.router import api_router
from app.infrastructure.database import init_db, close_db
# 导入所有 ORM 模型，确保 create_all 能发现全部表
import app.models  # noqa: F401

# 静态页面目录（隐私政策等公开法律文本）。
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    await init_db()
    yield
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
    allow_origins=["*"],  # 生产环境应限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "version": settings.APP_VERSION}


@app.get("/privacy", include_in_schema=False)
@app.get("/privacy.html", include_in_schema=False)
async def privacy_policy() -> FileResponse:
    """隐私政策页面，供 App 内跳转和应用商店审核使用。"""
    return FileResponse(STATIC_DIR / "privacy.html", media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
