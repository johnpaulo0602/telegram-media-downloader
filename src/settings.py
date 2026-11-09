import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Settings:
    api_id: int | None
    api_hash: str
    session_key: str
    download_dir: str = "downloads"
    batch_size: int = 5
    message_limit: int | None = 2000
    db_path: str = "tbmd.db"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_token: str | None = None

    def to_dict(self) -> dict:
        return {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "download_dir": self.download_dir,
            "batch_size": self.batch_size,
            "message_limit": self.message_limit,
            "db_path": self.db_path,
            "web_host": self.web_host,
            "web_port": self.web_port,
            "session_key": self.session_key,
            "web_token": self.web_token,
        }


def load_settings(env_file: str | None = None, load_env: bool = True) -> tuple[Settings | None, str | None]:
    """
    Load and validate environment variables into a typed Settings instance.
    Returns (Settings, None) on success or (None, error_message) on validation failure.
    """
    if load_env:
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    download_dir = os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads"
    batch_size_raw = os.getenv("BATCH_SIZE", "5")
    limit_raw = os.getenv("MESSAGE_LIMIT", "2000")
    db_path = os.getenv("DB_PATH", os.getenv("DB_FILE", "tbmd.db")).strip() or "tbmd.db"
    web_host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    web_port_raw = os.getenv("WEB_PORT", "8000")
    session_key = os.getenv("TBMD_SESSION_KEY", "").strip() or None
    web_token = os.getenv("TBMD_WEB_TOKEN", "").strip() or None

    if not api_id_raw or not api_id_raw.strip():
        return None, "API_ID não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

    try:
        api_id = int(api_id_raw.strip())
    except ValueError:
        return None, "API_ID precisa ser um número inteiro válido no .env."

    if not api_hash or not api_hash.strip():
        return None, "API_HASH não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

    if not session_key:
        return None, (
            "TBMD_SESSION_KEY não definido no .env. Defina uma senha forte para "
            "criptografar a sessão do Telegram armazenada no banco SQLite."
        )

    try:
        batch_size = max(1, int(batch_size_raw))
    except ValueError:
        batch_size = 5

    try:
        message_limit = int(limit_raw) if limit_raw and int(limit_raw) > 0 else None
    except ValueError:
        message_limit = 2000

    try:
        web_port = int(web_port_raw)
    except ValueError:
        web_port = 8000

    settings = Settings(
        api_id=api_id,
        api_hash=api_hash.strip(),
        download_dir=download_dir,
        batch_size=batch_size,
        message_limit=message_limit,
        db_path=db_path,
        web_host=web_host,
        web_port=web_port,
        session_key=session_key,
        web_token=web_token,
    )
    return settings, None
