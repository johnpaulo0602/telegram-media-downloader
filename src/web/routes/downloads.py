from fastapi import APIRouter, Query

from src.models import (
    PreviewRequest,
    QueuePauseRequest,
    StartDownloadRequest,
)
from src.web.manager import manager

router = APIRouter(tags=["downloads"])


@router.get("/api/status")
async def api_status():
    return manager.get_snapshot()


@router.post("/api/reconnect")
async def api_reconnect():
    await manager.initialize()
    return manager.get_snapshot()


@router.get("/api/dialogs")
async def api_dialogs(q: str = Query(default=""), refresh: bool = Query(default=False)):
    dialogs = await manager.get_dialogs_list(query=q, refresh=refresh)
    return {"dialogs": dialogs}


@router.post("/api/downloads/preview")
async def api_preview_downloads(req: PreviewRequest):
    return await manager.preview_media(req)


@router.post("/api/downloads/start")
async def api_start_downloads(req: StartDownloadRequest):
    return await manager.start_download_job(req)


@router.post("/api/downloads/cancel")
async def api_cancel_downloads():
    return await manager.cancel_job()


@router.post("/api/jobs/clear-completed")
async def api_clear_completed_jobs():
    return await manager.clear_completed_jobs()


@router.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job_by_id(job_id: str):
    return await manager.cancel_job(job_id)


@router.post("/api/jobs/{job_id}/delete")
async def api_delete_job_by_id(job_id: str):
    return await manager.delete_job(job_id)


@router.post("/api/jobs/{job_id}/move-up")
async def api_move_job_up(job_id: str):
    return await manager.move_job_up(job_id)


@router.post("/api/jobs/{job_id}/retry")
async def api_retry_job(job_id: str):
    return await manager.retry_job(job_id)


@router.post("/api/jobs/{job_id}/logs/clear")
async def api_clear_job_logs(job_id: str):
    job = manager._get_job(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    job["recent_logs"] = []
    if job_id == manager.current_job_id:
        manager.recent_logs.clear()
    manager.persist_queue_state()
    await manager.broadcast_state(force=True)
    return manager.get_snapshot()


@router.post("/api/queue/pause")
async def api_toggle_queue_pause(req: QueuePauseRequest | None = None):
    paused = req.paused if req is not None else None
    return await manager.toggle_queue_pause(paused)


@router.post("/api/logs/clear")
async def api_clear_logs():
    manager.recent_logs.clear()
    if manager.current_job_id:
        job = manager._get_job(manager.current_job_id)
        if job:
            job["recent_logs"] = []
    manager.persist_queue_state()
    await manager.broadcast_state(force=True)
    return manager.get_snapshot()
