import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Settings:
    api_id: int | None
    api_hash: str
    session_name: str = "default_session"
    download_dir: str = "downloads"
    batch_size: int = 5
    message_limit: int | None = 2000
    db_path: str = "tbmd.db"
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    def to_dict(self) -> dict:
        return {
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "session_name": self.session_name,
            "download_dir": self.download_dir,
            "batch_size": self.batch_size,
            "message_limit": self.message_limit,
            "db_path": self.db_path,
            "web_host": self.web_host,
            "web_port": self.web_port,
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
    session_name = os.getenv("SESSION_NAME", "default_session").strip() or "default_session"
    download_dir = os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads"
    batch_size_raw = os.getenv("BATCH_SIZE", "5")
    limit_raw = os.getenv("MESSAGE_LIMIT", "2000")
    db_path = os.getenv("DB_PATH", os.getenv("DB_FILE", "tbmd.db")).strip() or "tbmd.db"
    web_host = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    web_port_raw = os.getenv("WEB_PORT", "8000")

    if not api_id_raw or not api_id_raw.strip():
        return None, "API_ID não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

    try:
        api_id = int(api_id_raw.strip())
    except ValueError:
        return None, "API_ID precisa ser um número inteiro válido no .env."

    if not api_hash or not api_hash.strip():
        return None, "API_HASH não definido no .env. Copie o .env.example para .env e configure suas credenciais da API do Telegram."

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
        session_name=session_name,
        download_dir=download_dir,
        batch_size=batch_size,
        message_limit=message_limit,
        db_path=db_path,
        web_host=web_host,
        web_port=web_port,
    )
    return settings, None
