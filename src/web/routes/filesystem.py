import os
from fastapi import APIRouter, HTTPException, Query

from src.models import MkdirRequest
from src.utils import browse_directory, safe_filename
from src.web.manager import manager

router = APIRouter(prefix="/api/fs", tags=["filesystem"])


@router.get("")
async def api_browse_fs(
    path: str | None = Query(default=None),
    show_hidden: bool = Query(default=False),
):
    default_dir = (manager.config or {}).get("download_dir", "downloads")
    return browse_directory(path, default_dir=default_dir, show_hidden=show_hidden)


@router.post("/mkdir")
async def api_mkdir(req: MkdirRequest):
    safe_name = safe_filename(req.folder_name.strip())
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Nome de pasta inválido.")

    parent = os.path.abspath(os.path.expanduser(req.parent_path.strip() or "."))
    new_dir = os.path.join(parent, safe_name)
    try:
        os.makedirs(new_dir, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Sem permissão para criar pasta em '{parent}'.")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Erro ao criar pasta: {e}")

    default_dir = (manager.config or {}).get("download_dir", "downloads")
    return browse_directory(new_dir, default_dir=default_dir)
