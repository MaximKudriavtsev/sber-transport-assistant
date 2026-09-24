from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


class Settings(BaseSettings):
    app_name: str = "SBER Transport AI"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-3-Ultra"
    gigachat_verify_ssl: bool = False


    # Loading must not depend on the directory from which uvicorn/diagnostics is run.
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    @property
    def demo_mode(self) -> bool:
        return not bool(self.gigachat_credentials.strip())

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"development", "dev"}

    @property
    def sources_path(self) -> Path:
        return BASE_DIR / "data" / "sources.json"

    @property
    def chunks_path(self) -> Path:
        return BASE_DIR / "data" / "chunks.json"

    @property
    def source_snapshots_path(self) -> Path:
        return BASE_DIR / "data" / "source_snapshots.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
