"""客户端版本检测接口。"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings

router = APIRouter(tags=["App"])


class AppVersionResponse(BaseModel):
    """最新版本信息，客户端拿它和自己当前版本比较。"""

    latest_version: str
    min_version: str
    download_url: str
    note: str


@router.get("/app/version", response_model=AppVersionResponse)
async def get_app_version() -> AppVersionResponse:
    """客户端启动时检查更新，无需登录。"""
    return AppVersionResponse(
        latest_version=settings.APP_LATEST_VERSION,
        min_version=settings.APP_MIN_VERSION,
        download_url=settings.APP_UPDATE_URL,
        note=settings.APP_UPDATE_NOTE,
    )
