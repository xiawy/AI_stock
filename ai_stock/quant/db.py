"""Quant subsystem database engine & session.

与 backend 业务库合并后的统一 SQLite 存储:
- 默认 ``backend/data/aistock.db`` (与 backend ``app.core.database`` 共用同一
  文件, 可通过环境变量 ``QUANT_DB_URL`` 覆盖, 测试用 ``sqlite:///:memory:``
  或临时文件);
- 独立的 DeclarativeBase, 避免与 backend ``app.core.database.Base`` 的表
  注册互相干扰 (表名以 ``quant_`` 前缀隔离, 无冲突);
- 首次初始化时自动把旧 ``quant.db`` 的数据一次性搬运进来 (幂等);
- 多 Agent 消费线程共享一个 engine（``check_same_thread=False``），每个
  操作短事务; WAL 模式 + busy_timeout 保证双 engine 并发读写不互锁。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import QUANT_DB_URL_ENV

logger = logging.getLogger(__name__)

# 合并前的旧库文件名 (位于同目录; 迁移完成后保留文件本身, 仅搬运数据)
_LEGACY_QUANT_FILE = "quant.db"

# 旧榜单体系已废弃的表 (行业榜/热股榜改由 quant 选股流程产出)
_LEGACY_RANKING_TABLES = ("industry_rankings", "stock_recommendations")


class QuantBase(DeclarativeBase):
    """Declarative base shared by all quant ORM models."""


def _default_db_url() -> str:
    """默认统一 SQLite 路径: <project_root>/backend/data/aistock.db."""
    root = Path(__file__).resolve().parent.parent.parent  # ai_stock/quant -> project
    data_dir = root / "backend" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'aistock.db'}"


def _resolve_db_url() -> str:
    url = os.getenv(QUANT_DB_URL_ENV, "").strip()
    return url or _default_db_url()


def _apply_sqlite_pragmas(engine) -> None:
    """SQLite 连接级 PRAGMA: WAL + busy_timeout.

    quant 消费线程与 backend API 的两个 engine 指向同一文件时, WAL 允许
    一写多读并发, busy_timeout 避免瞬时锁竞争直接报 ``database is locked``。
    """
    if not str(engine.url).startswith("sqlite"):
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def _build_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    _apply_sqlite_pragmas(engine)
    return engine


_engine = None
_SessionFactory: sessionmaker | None = None
_engine_lock = threading.Lock()


def get_engine():
    """惰性创建并返回全局 quant engine 单例 (带锁, 防多线程重复初始化)."""
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine
    with _engine_lock:
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
    """建表 (幂等). 生产 schema 变更走 Alembic; 这里保持首跑零门槛.

    顺序: 建表 → 旧 quant.db 一次性搬运 → 多用户 schema 升级 →
    行业榜 schema 升级 → 删除废弃的旧榜单表。
    """
    import ai_stock.quant.db_models  # noqa: F401  确保表已注册

    engine = get_engine()
    needs_merge = _needs_legacy_merge(engine)
    QuantBase.metadata.create_all(bind=engine)
    if needs_merge:
        _merge_legacy_quant_db(engine)
    _migrate_multi_user_schema(engine)
    _migrate_industry_board_schema(engine)
    _migrate_pool_radar_schema(engine)
    _drop_legacy_ranking_tables(engine)
    logger.debug("Quant tables ensured on %s", engine.url)


def _needs_legacy_merge(engine) -> bool:
    """目标库尚无任何 quant 表, 且同目录存在旧 ``quant.db`` → 需要一次性合并."""
    url = str(engine.url)
    if not url.startswith("sqlite:///"):
        return False
    db_path = Path(url.removeprefix("sqlite:///"))
    if db_path.name == _LEGACY_QUANT_FILE:
        return False
    legacy = db_path.parent / _LEGACY_QUANT_FILE
    if not legacy.is_file():
        return False
    existing = set(inspect(engine).get_table_names())
    return not (set(QuantBase.metadata.tables) & existing)


def _merge_legacy_quant_db(engine) -> None:
    """把旧 ``quant.db`` 的全部 quant_* 表逐表搬运进目标库 (幂等, 短事务).

    只拷贝两侧共有的列 (容忍 schema 漂移); 目标表已由 create_all 建好。
    """
    url = str(engine.url)
    db_path = Path(url.removeprefix("sqlite:///"))
    legacy = db_path.parent / _LEGACY_QUANT_FILE
    logger.info("Merging legacy quant tables: %s -> %s", legacy, db_path)
    with engine.begin() as conn:
        conn.execute(text(f"ATTACH DATABASE '{legacy}' AS legacy"))
        try:
            legacy_tables = {
                row[0] for row in conn.execute(
                    text("SELECT name FROM legacy.sqlite_master WHERE type='table'")
                )
            }
            for table_name, table in QuantBase.metadata.tables.items():
                if table_name not in legacy_tables:
                    continue
                legacy_cols = {
                    row[1] for row in conn.execute(
                        text(f"PRAGMA legacy.table_info('{table_name}')")
                    )
                }
                shared = [c.name for c in table.columns if c.name in legacy_cols]
                if not shared:
                    continue
                col_list = ", ".join(f'"{c}"' for c in shared)
                result = conn.execute(text(
                    f'INSERT INTO main."{table_name}" ({col_list}) '
                    f'SELECT {col_list} FROM legacy."{table_name}"'
                ))
                logger.info(
                    "Legacy table migrated: %s (%d rows)",
                    table_name, result.rowcount or 0,
                )
        finally:
            conn.execute(text("DETACH DATABASE legacy"))


def _drop_legacy_ranking_tables(engine) -> None:
    """删除旧行业榜/热股榜表 (逻辑已迁至 quant 选股流程; 一次性清理)."""
    if not str(engine.url).startswith("sqlite"):
        return
    existing = set(inspect(engine).get_table_names())
    stale = [t for t in _LEGACY_RANKING_TABLES if t in existing]
    if not stale:
        return
    with engine.begin() as conn:
        for table_name in stale:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
            logger.info("Dropped legacy ranking table: %s", table_name)


def _migrate_industry_board_schema(engine) -> None:
    """存量库升级 (幂等): ``quant_industry_board`` 补 transmission_from
    (上游传导二次验证的来源行业标记, SQLite ADD COLUMN)."""
    if not str(engine.url).startswith("sqlite"):
        return
    insp = inspect(engine)
    if "quant_industry_board" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("quant_industry_board")}
    if "transmission_from" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE quant_industry_board ADD COLUMN "
            "transmission_from VARCHAR(64) NOT NULL DEFAULT ''"
        ))
    logger.info("quant_industry_board migrated: +transmission_from")


def _migrate_pool_radar_schema(engine) -> None:
    """存量库升级 (幂等): ``quant_stock_pool_optional`` 补主题雷达动态字段
    (dyn_confidence / stage_batch / last_confirmed / radar_theme / kicked_reason).

    旁路注入增强: 原 confidence (0-10 LLM 分) 语义不变; 新增列均带 DEFAULT,
    旧行自动获得退化基准值 (dyn_confidence=0.5, stage_batch='BODY')。
    """
    if not str(engine.url).startswith("sqlite"):
        return
    insp = inspect(engine)
    if "quant_stock_pool_optional" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("quant_stock_pool_optional")}
    adds = [
        ("dyn_confidence", "FLOAT NOT NULL DEFAULT 0.5"),
        ("stage_batch", "VARCHAR(8) NOT NULL DEFAULT 'BODY'"),
        ("last_confirmed", "DATETIME"),
        ("radar_theme", "VARCHAR(100) NOT NULL DEFAULT ''"),
        ("kicked_reason", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ]
    with engine.begin() as conn:
        for col, ddl in adds:
            if col not in cols:
                conn.execute(text(
                    f"ALTER TABLE quant_stock_pool_optional ADD COLUMN {col} {ddl}"
                ))
                logger.info("quant_stock_pool_optional migrated: +%s", col)


def _migrate_multi_user_schema(engine) -> None:
    """存量库升级多用户 schema (幂等):

    - ``quant_trade_log``: 补 user_id / cost_price / realized_pnl / pnl_pct
      (SQLite ADD COLUMN, 历史行默认 system 账户)
    - ``quant_stock_pool_holding``: 主键由 symbol 改为 (user_id, symbol),
      SQLite 无法原地改主键 → 旧表改名备份后重建 (模拟盘数据, 不迁移)
    """
    if not str(engine.url).startswith("sqlite"):
        return
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
