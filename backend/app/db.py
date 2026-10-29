from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _normalise_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _prepare_sqlite(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and url != "sqlite:///:memory:":
        filename = url[len(prefix) :]
        path = Path(filename)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)


_database_url = _normalise_url(get_settings().database_url)
_prepare_sqlite(_database_url)
_connect_args = {"check_same_thread": False} if _database_url.startswith("sqlite") else {}
_engine_options = {"connect_args": _connect_args, "pool_pre_ping": True}
if not _database_url.startswith("sqlite"):
    _engine_options.update(
        pool_size=get_settings().db_pool_size,
        max_overflow=get_settings().db_max_overflow,
        pool_timeout=get_settings().db_pool_timeout_seconds,
    )
engine = create_engine(_database_url, **_engine_options)
if _database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
