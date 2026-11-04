from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from src.web.manager import manager
from src.web.routes.downloads import router as downloads_router
from src.web.routes.filesystem import router as filesystem_router
from src.web.routes.queue import router as queue_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()
    yield
    await manager.shutdown()


app = FastAPI(
    title="Telegram Batch Media Downloader — Homelab UI",
    version="2.1.0",
    lifespan=lifespan,
)

# Static files path (supports src/static or src/web/static)
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Mount API routers
app.include_router(downloads_router)
app.include_router(queue_router)
app.include_router(filesystem_router)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>Interface não encontrada</h1>", status_code=404)
    return FileResponse(index_path)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    manager.ws_clients.add(ws)
    try:
        await ws.send_json(manager.get_snapshot())
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_json(manager.get_snapshot())
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.ws_clients.discard(ws)


def main():
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    host = os.getenv("WEB_HOST", "0.0.0.0")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run("src.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
