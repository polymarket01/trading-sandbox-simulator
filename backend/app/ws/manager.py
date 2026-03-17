from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self.public_subscriptions: dict[str, set[WebSocket]] = defaultdict(set)
        self.private_subscriptions: dict[tuple[int, str], set[WebSocket]] = defaultdict(set)
        self.authenticated_users: dict[WebSocket, int] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            for subscribers in self.public_subscriptions.values():
                subscribers.discard(websocket)
            for subscribers in self.private_subscriptions.values():
                subscribers.discard(websocket)
            self.authenticated_users.pop(websocket, None)

    async def auth_private(self, websocket: WebSocket, user_id: int) -> None:
        async with self._lock:
            self.authenticated_users[websocket] = user_id

    async def subscribe_public(self, websocket: WebSocket, channel: str, symbol: str, interval: str | None = None) -> None:
        key = f"{channel}:{symbol}:{interval or ''}"
        async with self._lock:
            self.public_subscriptions[key].add(websocket)

    async def subscribe_private(self, websocket: WebSocket, channel: str) -> None:
        async with self._lock:
            user_id = self.authenticated_users[websocket]
            self.private_subscriptions[(user_id, channel)].add(websocket)

    async def broadcast_public(self, channel: str, symbol: str, payload: dict, interval: str | None = None) -> None:
        key = f"{channel}:{symbol}:{interval or ''}"
        await self._broadcast(self.public_subscriptions[key], payload)

    async def broadcast_private(self, user_id: int, channel: str, payload: dict) -> None:
        await self._broadcast(self.private_subscriptions[(user_id, channel)], payload)

    async def _broadcast(self, sockets: set[WebSocket], payload: dict) -> None:
        stale: list[WebSocket] = []
        for websocket in list(sockets):
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    sockets.discard(websocket)
