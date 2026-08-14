"""SQLAlchemy engine / session / declarative base for SQLite."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _build_engine(settings: Settings):
    url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


settings = get_settings()

# Ensure the parent directory of the SQLite file exists (data/ by default).
if settings.database_url.startswith("sqlite:///"):
    from pathlib import Path

    db_path = settings.database_url.removeprefix("sqlite:///")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = _build_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables for models registered so far (dev convenience).

    Production schema changes go through Alembic (`alembic upgrade head`);
    this keeps first-run friction low for a SQLite-based deployment.
    """
    # Import models so their tables are registered on Base.metadata.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
