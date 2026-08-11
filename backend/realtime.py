"""
realtime.py — WebSocket для live-обновлений в UI.

Клиент (браузер) подключается к /ws, получает события в реальном времени:
- peer_added / peer_deleted
- handshake (новое подключение клиента)
- server_up / server_down
- traffic_threshold

События рассылаются ВСЕМ подключенным клиентам.
"""

import json
import asyncio
import threading
from typing import Set, Dict, Any
import fastapi
WebSocket = fastapi.WebSocket


class WSManager:
    """Менеджер WebSocket-подключений."""

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop = None
        self._lock = threading.Lock()

    def set_loop(self, loop):
        """Запоминаем event loop главного потока для schedule из background."""
        with self._lock:
            self._loop = loop

    async def connect(self, ws: WebSocket):
        await ws.accept()
        with self._lock:
            self._clients.add(ws)
        # Отправляем приветствие
        await self._safe_send(ws, {
            "event": "connected",
            "message": "WebSocket connected",
            "clients_count": len(self._clients)
        })

    async def disconnect(self, ws: WebSocket):
        with self._lock:
            self._clients.discard(ws)

    async def _safe_send(self, ws: WebSocket, data: Dict[str, Any]) -> bool:
        """Безопасная отправка с обработкой отключённого клиента."""
        try:
            await ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
            return True
        except Exception:
            with self._lock:
                self._clients.discard(ws)
            return False

    async def broadcast(self, data: Dict[str, Any]):
        """Рассылает событие всем подключённым клиентам."""
        with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        msg = json.dumps(data, ensure_ascii=False, default=str)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    def broadcast_async(self, data: Dict[str, Any]):
        """Безопасный вызов broadcast из любого потока.

        Если мы в event loop — использует asyncio.ensure_future.
        Иначе — run_coroutine_threadsafe.
        """
        with self._lock:
            loop = self._loop
            clients_count = len(self._clients)
        if clients_count == 0:
            return
        try:
            # Если вызов из event loop потока
            try:
                cur_loop = asyncio.get_running_loop()
                if cur_loop == loop:
                    # Тот же loop — используем ensure_future
                    asyncio.ensure_future(self.broadcast(data))
                    return
            except RuntimeError:
                pass  # нет running loop, используем schedule
            if not loop:
                return
            asyncio.run_coroutine_threadsafe(self.broadcast(data), loop)
        except Exception as e:
            print(f"ws broadcast error: {e}", file=__import__("sys").stderr)


# Singleton
ws_manager = WSManager()
