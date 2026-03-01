from __future__ import annotations

import asyncio
from typing import Dict, Set
from fastapi import WebSocket

class WSManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rooms: Dict[str, Set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, room: str) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms.setdefault(room, set()).add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            for room, conns in list(self._rooms.items()):
                if ws in conns:
                    conns.remove(ws)
                if not conns:
                    self._rooms.pop(room, None)

    async def broadcast(self, room: str, message: dict) -> None:
        async with self._lock:
            conns = list(self._rooms.get(room, set()))
        dead = []
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for d in dead:
                    for room2, conns2 in list(self._rooms.items()):
                        conns2.discard(d)
                        if not conns2:
                            self._rooms.pop(room2, None)

ws_manager = WSManager()
