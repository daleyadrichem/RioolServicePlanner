"""Reusable database utilities."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache
from typing import Final

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
    return create_engine(get_database_url(), echo=False, future=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """Return a cached SQLAlchemy session factory.

    Returns
    -------
    sessionmaker[Session]
        Session factory bound to the configured SQLAlchemy engine.
    """
    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


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
        yield session
        session.commit()
    except Exception:
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
        yield session
    finally:
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
