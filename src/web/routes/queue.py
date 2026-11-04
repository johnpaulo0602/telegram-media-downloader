from fastapi import APIRouter, Query

from src.downloader import get_queue_db
from src.web.manager import manager

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/stats")
async def api_queue_stats(chat_id: str | None = Query(default=None)):
    db = get_queue_db((manager.config or {}).get("db_path"))
    return {"stats": db.get_stats(chat_id)}


@router.post("/retry-failed")
async def api_queue_retry_failed(chat_id: str | None = Query(default=None)):
    db = get_queue_db((manager.config or {}).get("db_path"))
    count = db.retry_failed(chat_id)
    return {"retried_count": count, "stats": db.get_stats(chat_id)}


@router.get("/items")
async def api_queue_items(
    chat_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    db = get_queue_db((manager.config or {}).get("db_path"))
    items = db.list_items(chat_id=chat_id, status=status, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}
