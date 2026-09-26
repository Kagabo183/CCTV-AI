from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _enforce_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """SQLite ignores foreign keys (and so every ON DELETE CASCADE) unless each connection turns them on."""
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        # a writer waits for the lock instead of failing at once with "database is locked" (set first: the
        # statements below may need the lock themselves)
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        # many live cameras write events while the API reads and deletes: with WAL, readers never block the
        # writer. WAL is stored in the database file, so it is only switched on once (switching needs an
        # exclusive lock and would fail while another connection is writing).
        cursor.execute("PRAGMA journal_mode")
        if (cursor.fetchone() or [""])[0].lower() != "wal":
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:  # noqa: BLE001 - another connection is busy: the next connection switches it
                pass
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _enforce_sqlite_foreign_keys(_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def override_engine(engine: AsyncEngine) -> None:
    """Used by tests to swap in a different database."""
    global _engine, _sessionmaker
    _enforce_sqlite_foreign_keys(engine)
    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
