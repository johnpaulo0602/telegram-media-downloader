from contextlib import asynccontextmanager
import os
import secrets

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.web.manager import manager
from src.web.routes.auth import router as auth_router
from src.web.routes.downloads import router as downloads_router
from src.web.routes.filesystem import router as filesystem_router
from src.web.routes.queue import router as queue_router

TOKEN_COOKIE = "tbmd_token"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.initialize()
    yield
    await manager.shutdown()


app = FastAPI(
    title="Telegram Batch Media Downloader — Homelab UI",
    version="2.2.0",
    lifespan=lifespan,
)

# Static files path (supports src/static or src/web/static)
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# Mount API routers
app.include_router(auth_router)
app.include_router(downloads_router)
app.include_router(queue_router)
app.include_router(filesystem_router)

# Serve the frontend assets (logo, favicon)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def expected_token() -> str | None:
    """The access token protecting the panel, when one is configured."""
    return (manager.config or {}).get("web_token")


def token_is_valid(candidate: str | None) -> bool:
    expected = expected_token()
    if not expected or not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


@app.middleware("http")
async def enforce_access_token(request, call_next):
    """
    Guard the API with TBMD_WEB_TOKEN when it is configured.

    The panel page itself is always served so the frontend can ask for the
    token; every /api call must carry it, either in the cookie or in the
    X-Auth-Token header.
    """
    if request.method == "GET" and request.url.path == "/":
        query_token = request.query_params.get("token")
        if token_is_valid(query_token):
            response = RedirectResponse("/", status_code=302)
            response.set_cookie(TOKEN_COOKIE, query_token, httponly=True, samesite="lax", path="/")
            return response

    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    if request.url.path == "/api/auth/token":
        return await call_next(request)

    provided = request.cookies.get(TOKEN_COOKIE) or request.headers.get("X-Auth-Token")
    if expected_token() and not token_is_valid(provided):
        return JSONResponse(
            status_code=401,
            content={"detail": "Token de acesso invalido ou ausente."},
        )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>Interface não encontrada</h1>", status_code=404)
    return FileResponse(index_path)


@app.post("/api/auth/token")
async def api_set_access_token(payload: dict):
    """Validate the access token and store it in an httpOnly cookie."""
    token = (payload or {}).get("token", "")
    if not expected_token():
        return {"ok": True, "required": False}
    if not token_is_valid(token):
        raise HTTPException(status_code=401, detail="Token de acesso invalido.")
    response = JSONResponse({"ok": True, "required": True})
    response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="lax", path="/")
    return response


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if expected_token() and not token_is_valid(ws.cookies.get(TOKEN_COOKIE)):
        await ws.close(code=1008)
        return
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
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run("src.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
