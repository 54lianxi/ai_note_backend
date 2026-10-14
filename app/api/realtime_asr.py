"""实时语音转写 WebSocket 接口。"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import get_websocket_user_id
from app.infrastructure.database import async_session_factory
from app.services.realtime_asr_service import RealtimeASRService
from app.services.subscription_service import is_subscribed

router = APIRouter(tags=["实时语音转写"])
logger = logging.getLogger(__name__)


@router.websocket("/records/voice/realtime")
async def realtime_transcription(websocket: WebSocket):
    token = websocket.query_params.get("token")
    try:
        user_id = get_websocket_user_id(token)
    except Exception:
        await websocket.close(code=4401, reason="未登录")
        return

    # 实时转写是订阅功能：未订阅直接拒绝，省得白跑一趟上游模型。
    async with async_session_factory() as session:
        subscribed = await is_subscribed(session, user_id)
    if not subscribed:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "subscription_required"})
        await websocket.close(code=4403, reason="需要订阅")
        return

    await websocket.accept()
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def frames():
        while True:
            frame = await audio_queue.get()
            if frame is None:
                return
            yield frame

    async def receive_audio():
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect
            if message.get("bytes") is not None:
                await audio_queue.put(message["bytes"])
            elif message.get("text") == "stop":
                await audio_queue.put(None)
                return
            elif message.get("text") == "cancel":
                await audio_queue.put(None)
                return

    receiver = asyncio.create_task(receive_audio())
    try:
        async for event in RealtimeASRService().transcribe(frames()):
            await websocket.send_json(event)
        await receiver
    except WebSocketDisconnect:
        receiver.cancel()
    except Exception as error:
        logger.exception("实时语音转写失败")
        if websocket.client_state.name == "CONNECTED":
            await websocket.send_json({"type": "error", "message": str(error)})
    finally:
        if not receiver.done():
            receiver.cancel()
