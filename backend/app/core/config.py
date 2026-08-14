"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory (this file lives at backend/app/core/config.py)
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
# Project root (ai-stock/), reused as the analysis engine.
PROJECT_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────
    app_name: str = "AI Stock API"
    debug: bool = False

    # ── Security ─────────────────────────────────────────────────────────
    # openssl rand -hex 32
    secret_key: str = "dev-secret-key-change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24h

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'aistock.db'}"

    # ── CORS ─────────────────────────────────────────────────────────────
    # Vite dev server (5173) and the production static build origin.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── Analysis engine (original project) ───────────────────────────────
    # The engine packages stay in place and importable as a library.
    project_root: str = str(PROJECT_ROOT)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
