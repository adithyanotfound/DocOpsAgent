from collections import defaultdict

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self.active: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, workspace_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active[workspace_id].append(websocket)

    def disconnect(self, workspace_id: str, websocket: WebSocket) -> None:
        if websocket in self.active[workspace_id]:
            self.active[workspace_id].remove(websocket)

    async def send(self, workspace_id: str, event: dict) -> None:
        stale: list[WebSocket] = []
        for websocket in self.active[workspace_id]:
            try:
                await websocket.send_json(event)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(workspace_id, websocket)

    async def progress(self, workspace_id: str, message: str) -> None:
        await self.send(workspace_id, {"type": "progress", "message": message})


manager = WebSocketManager()
