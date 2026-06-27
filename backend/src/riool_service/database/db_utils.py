"""Reusable database utilities."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.orm import Session, sessionmaker

from riool_service.debug_logging import (
    current_stack,
    log_debug_event,
    log_exception_event,
    safe_repr,
)

load_dotenv()

DEFAULT_DATABASE_URL: Final[str] = "sqlite:///./app.db"


def get_database_url() -> str:
    """Return the configured database URL.

    Returns
    -------
    str
        The database URL from the ``DATABASE_URL`` environment variable, or the
        default SQLite URL when the environment variable is not set.
    """
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine for the configured database.

    Returns
    -------
    Engine
        SQLAlchemy engine created from the configured database URL.
    """
    engine = create_engine(get_database_url(), echo=False, future=True)
    _install_engine_logging(engine)
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return a cached SQLAlchemy session factory.

    Returns
    -------
    sessionmaker[Session]
        Session factory bound to the configured SQLAlchemy engine.
    """
    factory = sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    _install_session_logging(factory)
    return factory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Provide a transactional SQLAlchemy session scope.

    Yields
    ------
    Session
        Active SQLAlchemy ORM session.

    Raises
    ------
    Exception
        Re-raises any exception raised inside the context after rolling back
        the transaction.

    Examples
    --------
    >>> with session_scope() as session:
    ...     session.add(my_model)
    """
    session_factory = get_session_factory()
    session = session_factory()

    try:
        log_debug_event(
            "db.session_scope.open",
            session_id=id(session),
            stack=current_stack(skip=1),
        )
        yield session
        log_debug_event(
            "db.session_scope.commit.request",
            session_id=id(session),
            stack=current_stack(skip=1),
        )
        session.commit()
    except Exception as exc:
        log_exception_event(
            "db.session_scope.rollback",
            exc,
            session_id=id(session),
            stack=current_stack(skip=1),
        )
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """Yield a database session.

    Yields
    ------
    Session
        SQLAlchemy ORM session.

    Notes
    -----
    This function is suitable as a FastAPI dependency.
    """
    session_factory = get_session_factory()
    session = session_factory()

    try:
        log_debug_event(
            "db.session.open",
            session_id=id(session),
            stack=current_stack(skip=1),
        )
        yield session
    finally:
        log_debug_event(
            "db.session.close",
            session_id=id(session),
            stack=current_stack(skip=1),
        )
        session.close()


def reset_database_utils_cache() -> None:
    """Clear cached database engine and session factory instances.

    Returns
    -------
    None
        Clears internal caches used by ``get_engine`` and
        ``get_session_factory``.

    Notes
    -----
    This is mainly useful in tests when changing ``DATABASE_URL`` between test
    cases.
    """
    get_engine.cache_clear()
    get_session_factory.cache_clear()


_WRITE_SQL_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "CREATE",
    "ALTER",
    "DROP",
    "TRUNCATE",
    "REPLACE",
)


def _is_write_statement(statement: str) -> bool:
    stripped = statement.lstrip().upper()
    return stripped.startswith(_WRITE_SQL_PREFIXES)


def _install_engine_logging(engine: Engine) -> None:
    """Install SQL-level logging for writes on a single engine."""
    if getattr(engine, "_riool_debug_logging_installed", False):
        return

    @event.listens_for(engine, "before_cursor_execute")
    def _log_before_cursor_execute(
        conn,  # type: ignore[no-untyped-def]
        cursor,  # type: ignore[no-untyped-def]
        statement: str,
        parameters,  # type: ignore[no-untyped-def]
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if not _is_write_statement(statement):
            return

        log_debug_event(
            "db.sql.write.before",
            connection_id=id(conn),
            statement=statement,
            parameters=safe_repr(parameters),
            executemany=executemany,
            stack=current_stack(skip=1),
        )

    @event.listens_for(engine, "after_cursor_execute")
    def _log_after_cursor_execute(
        conn,  # type: ignore[no-untyped-def]
        cursor,  # type: ignore[no-untyped-def]
        statement: str,
        parameters,  # type: ignore[no-untyped-def]
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if not _is_write_statement(statement):
            return

        log_debug_event(
            "db.sql.write.after",
            connection_id=id(conn),
            statement=statement,
            parameters=safe_repr(parameters),
            executemany=executemany,
            rowcount=getattr(cursor, "rowcount", None),
            stack=current_stack(skip=1),
        )

    @event.listens_for(engine, "handle_error")
    def _log_sql_error(exception_context) -> None:  # type: ignore[no-untyped-def]
        statement = exception_context.statement or ""
        if not _is_write_statement(statement):
            return

        log_exception_event(
            "db.sql.write.error",
            exception_context.original_exception,
            statement=statement,
            parameters=safe_repr(exception_context.parameters),
            stack=current_stack(skip=1),
        )

    setattr(engine, "_riool_debug_logging_installed", True)


def _object_debug_state(obj: object) -> dict[str, str]:
    return {
        "class": f"{obj.__class__.__module__}.{obj.__class__.__name__}",
        "repr": safe_repr(obj),
    }


def _install_session_logging(factory: sessionmaker[Session]) -> None:
    """Install ORM/session-level logging for a session factory."""
    if getattr(factory, "_riool_debug_logging_installed", False):
        return

    @event.listens_for(factory, "before_flush")
    def _log_before_flush(session: Session, flush_context, instances) -> None:  # type: ignore[no-untyped-def]
        new = [_object_debug_state(obj) for obj in session.new]
        dirty = [_object_debug_state(obj) for obj in session.dirty]
        deleted = [_object_debug_state(obj) for obj in session.deleted]
        if not new and not dirty and not deleted:
            return

        log_debug_event(
            "db.orm.flush.before",
            session_id=id(session),
            new=new,
            dirty=dirty,
            deleted=deleted,
            stack=current_stack(skip=1),
        )

    @event.listens_for(factory, "after_flush")
    def _log_after_flush(session: Session, flush_context) -> None:  # type: ignore[no-untyped-def]
        log_debug_event(
            "db.orm.flush.after",
            session_id=id(session),
            stack=current_stack(skip=1),
        )

    @event.listens_for(factory, "before_commit")
    def _log_before_commit(session: Session) -> None:
        log_debug_event(
            "db.transaction.commit.before",
            session_id=id(session),
            new_count=len(session.new),
            dirty_count=len(session.dirty),
            deleted_count=len(session.deleted),
            stack=current_stack(skip=1),
        )

    @event.listens_for(factory, "after_commit")
    def _log_after_commit(session: Session) -> None:
        log_debug_event(
            "db.transaction.commit.after",
            session_id=id(session),
            stack=current_stack(skip=1),
        )

    @event.listens_for(factory, "after_rollback")
    def _log_after_rollback(session: Session) -> None:
        log_debug_event(
            "db.transaction.rollback.after",
            session_id=id(session),
            stack=current_stack(skip=1),
        )

    setattr(factory, "_riool_debug_logging_installed", True)
