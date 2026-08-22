"""Database engine and session configuration."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from .models import Base


def create_session_factory(
    url: str,
    *,
    create_schema: bool = True,
) -> sessionmaker[Session]:
    """Create a session factory with safe SQLite defaults.

    ``create_schema`` remains enabled for backwards compatibility and isolated
    tests. Deployed databases should be created and upgraded with Alembic, then
    pass ``create_schema=False``.
    """
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def pragmas(connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    if create_schema:
        Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
