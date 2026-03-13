from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.helpers import public_room_for_checkpoint
from app.ws_manager import ws_manager

router = APIRouter()


@router.websocket("/ws/public")
async def ws_public(ws: WebSocket):
    checkpoint_id = ws.query_params.get("checkpoint_id")

    room = public_room_for_checkpoint(checkpoint_id)
    await ws_manager.connect(ws, room=room)

    await ws.send_json({
        "type": "hello",
        "room": room,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })

    ping_task = asyncio.create_task(_ws_ping_loop(ws))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ping_task.cancel()
        await ws_manager.disconnect(ws)


async def _ws_ping_loop(ws: WebSocket):
    while True:
        await asyncio.sleep(25)
        try:
            await ws.send_json({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()})
        except Exception:
            break
