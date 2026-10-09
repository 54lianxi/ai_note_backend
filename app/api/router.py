"""API 路由汇总"""
from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.app_info import router as app_info_router
from app.api.chat import router as chat_router
from app.api.record import router as record_router
from app.api.search import router as search_router
from app.api.realtime_asr import router as realtime_asr_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(app_info_router)
api_router.include_router(chat_router)
api_router.include_router(record_router)
api_router.include_router(search_router)
api_router.include_router(realtime_asr_router)
