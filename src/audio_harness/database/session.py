from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


DATABASE_BASELINE_REVISION = "0001_initial"


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        raise ValueError("第一版数据库只支持 SQLite")
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(
        parents=True,
        exist_ok=True,
    )


def _sync_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername != "sqlite+aiosqlite":
        raise ValueError("数据库 URL 必须使用 sqlite+aiosqlite")
    return str(url.set(drivername="sqlite"))


def upgrade_database(database_url: str) -> None:
    _ensure_sqlite_parent(database_url)
    migrations = Path(__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", _sync_database_url(database_url))
    command.upgrade(config, "head")


def require_fresh_database(database_url: str) -> None:
    """只接受空库或当前基线数据库，不提供任何历史数据迁移。"""

    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        raise ValueError("第一版数据库只支持 SQLite")
    if not url.database or url.database == ":memory:":
        return
    database_path = Path(url.database).expanduser().resolve()
    if not database_path.exists() or database_path.stat().st_size == 0:
        return
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables:
            return
        if "alembic_version" not in tables:
            raise RuntimeError(
                f"数据库 {database_path} 不是可识别的 Audio Harness 数据库；"
                "请人工移走该文件后重新启动"
            )
        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    revision = revision_row[0] if revision_row is not None else None
    if revision != DATABASE_BASELINE_REVISION:
        raise RuntimeError(
            "当前版本不支持原地升级旧业务数据。请停止服务并人工移走 "
            f"{database_path}、{database_path}-wal 和 {database_path}-shm，"
            "再重新启动以创建干净数据库"
        )


class Database:
    def __init__(self, database_url: str) -> None:
        _ensure_sqlite_parent(database_url)
        self.engine: AsyncEngine = create_async_engine(database_url)

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")

    async def close(self) -> None:
        await self.engine.dispose()
