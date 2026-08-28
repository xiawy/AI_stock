"""Quant subsystem database engine & session.

独立的 SQLite 存储（与 backend 业务库分离）：
- 默认 ``backend/data/quant.db``（相对项目根，可通过环境变量 ``QUANT_DB_URL``
  覆盖，测试用 ``sqlite:///:memory:`` 或临时文件）；
- 独立的 DeclarativeBase，避免与 backend ``app.core.database.Base`` 的表
  注册互相干扰；
- 多 Agent 消费线程共享一个 engine（``check_same_thread=False``），每个
  操作短事务。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import QUANT_DB_URL_ENV

logger = logging.getLogger(__name__)


class QuantBase(DeclarativeBase):
    """Declarative base shared by all quant ORM models."""


def _default_db_url() -> str:
    """默认 quant SQLite 路径: <project_root>/backend/data/quant.db."""
    root = Path(__file__).resolve().parent.parent.parent  # ai_stock/quant -> project
    data_dir = root / "backend" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'quant.db'}"


def _resolve_db_url() -> str:
    url = os.getenv(QUANT_DB_URL_ENV, "").strip()
    return url or _default_db_url()


def _build_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)


_engine = None
_SessionFactory: sessionmaker | None = None


def get_engine():
    """惰性创建并返回全局 quant engine 单例."""
    global _engine, _SessionFactory
    if _engine is None:
        url = _resolve_db_url()
        _engine = _build_engine(url)
        _SessionFactory = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False,
        )
        logger.info("Quant engine ready: %s", url)
    return _engine


def reset_engine(url: str) -> None:
    """测试辅助: 强制重建 engine (新建连接指向新 DB)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(url)
    _SessionFactory = sessionmaker(
        bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False,
    )


def get_session() -> Generator[Session, None, None]:
    """Yield 一个 scoped session (with 语义, 自动 close)."""
    get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
    finally:
        session.close()


def session_scope() -> Session:
    """直接返回一个 session (调用方负责 close); 便于非 generator 上下文."""
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


def init_quant_db() -> None:
    """建表 (幂等). 生产 schema 变更走 Alembic; 这里保持首跑零门槛."""
    import ai_stock.quant.db_models  # noqa: F401  确保表已注册

    engine = get_engine()
    QuantBase.metadata.create_all(bind=engine)
    _migrate_multi_user_schema(engine)
    logger.debug("Quant tables ensured on %s", engine.url)


def _migrate_multi_user_schema(engine) -> None:
    """存量库升级多用户 schema (幂等):

    - ``quant_trade_log``: 补 user_id / cost_price / realized_pnl / pnl_pct
      (SQLite ADD COLUMN, 历史行默认 system 账户)
    - ``quant_stock_pool_holding``: 主键由 symbol 改为 (user_id, symbol),
      SQLite 无法原地改主键 → 旧表改名备份后重建 (模拟盘数据, 不迁移)
    """
    if not str(engine.url).startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())

    if "quant_trade_log" in tables:
        cols = {c["name"] for c in insp.get_columns("quant_trade_log")}
        adds = [
            ("user_id", "VARCHAR(32) NOT NULL DEFAULT 'system'"),
            ("cost_price", "FLOAT DEFAULT 0"),
            ("realized_pnl", "FLOAT DEFAULT 0"),
            ("pnl_pct", "FLOAT DEFAULT 0"),
        ]
        with engine.begin() as conn:
            for col, ddl in adds:
                if col not in cols:
                    conn.execute(text(
                        f"ALTER TABLE quant_trade_log ADD COLUMN {col} {ddl}"
                    ))
                    logger.info("quant_trade_log migrated: +%s", col)

    if "quant_stock_pool_holding" in tables:
        cols = {c["name"] for c in insp.get_columns("quant_stock_pool_holding")}
        if "user_id" not in cols:
            import time

            backup = f"quant_stock_pool_holding_legacy_{int(time.time())}"
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE quant_stock_pool_holding RENAME TO {backup}"
                ))
            QuantBase.metadata.tables["quant_stock_pool_holding"].create(bind=engine)
            logger.warning(
                "quant_stock_pool_holding 主键升级为 (user_id, symbol), "
                "旧表已备份为 %s (模拟盘数据不迁移)", backup,
            )
