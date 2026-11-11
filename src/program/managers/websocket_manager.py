import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger


class ConnectionManager:
    """Manage per-topic WebSocket subscribers and bounded broadcast queues."""

    def __init__(self) -> None:
        self.active_connections = dict[str, list[WebSocket]]()
        self.message_queues = dict[str, asyncio.Queue[Any]]()
        self.background_tasks = list[asyncio.Task[Any]]()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, websocket: WebSocket, topic: str) -> None:
        self._loop = asyncio.get_running_loop()
        await websocket.accept()

        if topic not in self.active_connections:
            self.active_connections[topic] = []

        self.active_connections[topic].append(websocket)

        if topic not in self.message_queues:
            self.message_queues[topic] = asyncio.Queue(maxsize=500)
            task = asyncio.create_task(
                self._broadcast_messages(topic),
                name=f"broadcast_{topic}",
            )
            self.background_tasks.append(task)

    async def disconnect(self, websocket: WebSocket, topic: str) -> None:
        if topic not in self.active_connections:
            return

        if websocket in self.active_connections[topic]:
            self.active_connections[topic].remove(websocket)

        if not self.active_connections[topic]:
            del self.active_connections[topic]
            self.message_queues.pop(topic, None)

            target_name = f"broadcast_{topic}"
            for task in list(self.background_tasks):
                if task.get_name() == target_name:
                    task.cancel()
                    self.background_tasks.remove(task)
                    break

    def _enqueue_on_loop(self, topic: str, formatted_message: dict[str, Any]) -> None:
        queue = self.message_queues.get(topic)
        if queue is None:
            return
        try:
            queue.put_nowait(formatted_message)
        except asyncio.QueueFull:
            logger.warning(f"WebSocket message queue full for topic {topic}")

    def publish(self, topic: str, message: Any) -> None:
        """Publish a message to a specific topic safely from any thread."""
        if topic not in self.message_queues:
            return

        formatted_message = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": message,
        }

        loop = self._loop
        if loop is not None:
            try:
                if asyncio.get_running_loop() == loop:
                    self._enqueue_on_loop(topic, formatted_message)
                    return
            except RuntimeError:
                pass
            if not loop.is_closed():
                loop.call_soon_threadsafe(
                    self._enqueue_on_loop, topic, formatted_message
                )
                return

        self._enqueue_on_loop(topic, formatted_message)

    async def _broadcast_messages(self, topic: str) -> None:
        """Background task to broadcast messages for a specific topic."""
        try:
            while True:
                queue = self.message_queues.get(topic)
                if queue is None:
                    break
                message = await queue.get()

                connections = list(self.active_connections.get(topic, ()))
                if not connections:
                    continue

                dead_connections = list[WebSocket]()
                for connection in connections:
                    try:
                        await connection.send_json(message)
                    except WebSocketDisconnect:
                        dead_connections.append(connection)
                    except Exception as e:
                        logger.debug(f"Error sending WebSocket message on {topic}: {e}")
                        dead_connections.append(connection)

                for dead in dead_connections:
                    await self.disconnect(dead, topic)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Broadcast task error for topic {topic}: {e}")


manager = ConnectionManager()
