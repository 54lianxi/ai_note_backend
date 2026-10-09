"""实时语音转写 WebSocket 接口。"""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import get_websocket_user_id
from app.services.realtime_asr_service import RealtimeASRService

router = APIRouter(tags=["实时语音转写"])
logger = logging.getLogger(__name__)


@router.websocket("/records/voice/realtime")
async def realtime_transcription(websocket: WebSocket):
    token = websocket.query_params.get("token")
    try:
        get_websocket_user_id(token)
    except Exception:
        await websocket.close(code=4401, reason="未登录")
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
