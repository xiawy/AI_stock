"""Application settings loaded from environment / .env."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ directory (this file lives at backend/app/core/config.py)
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
# Project root (ai-stock/), reused as the analysis engine.
PROJECT_ROOT = BACKEND_DIR.parent

# 引擎（ai_stock）读 LLM key / provider 的约定是「项目根目录 .env」（README
# 「LLM Key」一节），backend 服务变量（SECRET_KEY 等）也已合并进去——
# 根 .env 是唯一事实来源。backend/.env 若存在则优先级更高，仅作本地
# 覆盖通道。在任何引擎模块 import 之前装载两者进 os.environ；
# override=False：已有环境变量最优先，backend/.env 其次，根 .env 仅补充
# （与下方 Settings.env_file 列表的优先级语义一致）。
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Sentinel default — get_settings() warns when this is still in use.
_DEFAULT_SECRET_KEY = "dev-secret-key-change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Single source of truth: project-root .env. An optional
        # backend/.env (later in the list) takes precedence for local
        # overrides; a missing file is ignored silently.
        env_file=[
            str(PROJECT_ROOT / ".env"),
            str(BACKEND_DIR / ".env"),
        ],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────
    app_name: str = "AI Stock API"
    debug: bool = False

    # ── Security ─────────────────────────────────────────────────────────
    # openssl rand -hex 32
    secret_key: str = _DEFAULT_SECRET_KEY
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
    settings = Settings()
    if settings.secret_key == _DEFAULT_SECRET_KEY:
        logging.getLogger(__name__).warning(
            "SECRET_KEY is using its default value — JWTs are forgeable by anyone. "
            "Set SECRET_KEY in backend/.env (openssl rand -hex 32) before deploying."
        )
    return settings
