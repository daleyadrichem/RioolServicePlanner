"""Database creation and schema helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from riool_service.database.models.tickets import Base

# Imported so SQLAlchemy registers these tables in Base.metadata.create_all().
from riool_service.database.models.branch import Branch as _Branch  # noqa: F401
from riool_service.database.models.location import Location as _Location  # noqa: F401
from riool_service.database.models.planning_assignment import (  # noqa: F401
    PlanningAssignment as _PlanningAssignment,
)
from riool_service.database.models.planning_run import PlanningRun as _PlanningRun  # noqa: F401
from riool_service.database.models.requirement import Requirement as _Requirement  # noqa: F401
from riool_service.database.models.route_cache import RouteCache as _RouteCache  # noqa: F401
from riool_service.database.models.technician import Technician as _Technician  # noqa: F401
from riool_service.database.models.technician_requirement import (  # noqa: F401
    TechnicianRequirement as _TechnicianRequirement,
)
from riool_service.database.models.ticket_requirement import (  # noqa: F401
    TicketRequirement as _TicketRequirement,
)
from riool_service.database.models.ticket_subjects import (  # noqa: F401
    TicketSubject as _TicketSubject,
)


def create_database_if_missing(database_url: str) -> None:
    """Create the physical database when supported."""
    url = make_url(database_url)

    if url.drivername.startswith("sqlite"):
        if url.database and url.database not in {":memory:", ""}:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return

    if not url.drivername.startswith("postgresql"):
        return

    database_name = url.database
    if not database_name:
        return

    maintenance_url = url.set(database="postgres")
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT", future=True)

    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            ).scalar_one_or_none()

            if exists is None:
                safe_database_name = database_name.replace('"', '""')
                connection.execute(text(f'CREATE DATABASE "{safe_database_name}"'))
    finally:
        engine.dispose()


def create_schema(engine: Engine) -> None:
    """Create all database tables from SQLAlchemy metadata."""
    Base.metadata.create_all(engine)
