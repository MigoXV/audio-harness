from __future__ import annotations

from pathlib import Path

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
