from fastapi import APIRouter, Body, HTTPException

from src.models import CodeRequest, PasswordRequest, PhoneRequest, UnlockRequest
from src.web.auth import AuthError
from src.web.manager import manager

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _guard() -> None:
    """Raise a 400 when the server has no Telegram credentials configured."""
    if not manager.config:
        raise HTTPException(status_code=400, detail="Credenciais do Telegram não configuradas.")


@router.get("/state")
async def api_auth_state():
    """Current authentication state, used by the frontend to pick a screen."""
    return manager.auth.state()


@router.post("/unlock")
async def api_auth_unlock(req: UnlockRequest):
    """Decrypt the stored session with the given passphrase."""
    _guard()
    try:
        result = await manager.auth.unlock(req.passphrase)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.post("/phone")
async def api_auth_phone(req: PhoneRequest):
    """Send the Telegram login code to the given phone number."""
    _guard()
    try:
        result = await manager.auth.request_code(req.phone, req.passphrase)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.post("/code")
async def api_auth_code(req: CodeRequest):
    """Submit the login code received on Telegram."""
    _guard()
    try:
        result = await manager.auth.submit_code(req.code)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.post("/password")
async def api_auth_password(req: PasswordRequest):
    """Complete the login with the two-factor authentication password."""
    _guard()
    try:
        result = await manager.auth.submit_password(req.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.post("/qr")
async def api_auth_qr(req: UnlockRequest | None = Body(default=None)):
    """Start a QR login and return the image to display."""
    _guard()
    try:
        result = await manager.auth.start_qr(req.passphrase if req else None)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.get("/qr/start")
async def api_auth_qr_start():
    """Start a QR login without a body, using the configured passphrase."""
    _guard()
    try:
        result = await manager.auth.start_qr()
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state(force=True)
    return result


@router.get("/qr/status")
async def api_auth_qr_status():
    """Report the QR login progress."""
    _guard()
    return await manager.auth.qr_status()


@router.post("/logout")
async def api_auth_logout():
    """Revoke the Telegram session and erase it from the database."""
    result = await manager.auth.logout()
    manager.client = None
    manager.connected = False
    manager.authorized = False
    manager.user_info = None
    await manager.broadcast_state(force=True)
    return result
