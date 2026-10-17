"""SQLAlchemy engine / session / declarative base for SQLite.

与 quant 子系统共用同一个 SQLite 文件 (默认 ``data/aistock.db``):
quant 侧 engine 也指向本库, WAL + busy_timeout 保证双 engine 并发读写。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _build_engine(settings: Settings):
    url = settings.database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if url.startswith("sqlite"):
        # 与 quant engine 同库共存: WAL 允许一写多读, busy_timeout 避免瞬时锁竞争。
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


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
    _migrate_analysis_task_state(engine)


# A5: analysis_tasks 的跨 worker 状态列。create_all 不会给**已存在**的表补列,
# 而现网库是 init_db() 建起来的, 因此这里做一次幂等补列 (与 alembic 版本对齐)。
_ANALYSIS_TASK_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("progress_json", "TEXT DEFAULT '{}'"),
    ("result_json", "TEXT DEFAULT ''"),
    ("owner", "VARCHAR(96) DEFAULT ''"),
    ("heartbeat_at", "DATETIME"),
    ("control", "VARCHAR(16) DEFAULT ''"),
    ("control_at", "DATETIME"),
)


def _migrate_analysis_task_state(bind) -> None:
    """幂等补列：老库升级到 A5 后仍能直接启动，无需手工跑迁移。"""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "analysis_tasks" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("analysis_tasks")}
    missing = [(name, ddl) for name, ddl in _ANALYSIS_TASK_STATE_COLUMNS if name not in existing]
    if not missing:
        return

    with bind.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE analysis_tasks ADD COLUMN {name} {ddl}"))
        # SQLite 只能在建列后单独建索引
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_analysis_tasks_heartbeat_at "
                "ON analysis_tasks (heartbeat_at)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_analysis_tasks_owner "
                "ON analysis_tasks (owner)"
            )
        )
