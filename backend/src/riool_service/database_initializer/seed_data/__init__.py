"""Seed static domain data, technicians and reusable simulated locations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from riool_service.database.models.planning_assignment import PlanningAssignment
from riool_service.database.models.planning_run import PlanningRun

from .branch import seed_original_branch
from .locations import seed_locations_from_csv, seed_simulated_locations
from .requirements import seed_default_requirements
from .technicians import seed_technicians


LOCATION_SOURCE_CSV = "csv"
LOCATION_SOURCE_RANDOM = "random"


def clear_planning_history(session: Session) -> None:
    """Remove old planner output before reseeding static data.

    Initializer runs should leave the app in a clean planning state. Delete
    assignments first because they reference planning runs.
    """
    session.execute(delete(PlanningAssignment))
    session.execute(delete(PlanningRun))
    session.flush()


def seed_database(
    session: Session,
    *,
    technicians_config: dict[str, Any],
    locations_config: dict[str, Any],
    location_source: str = LOCATION_SOURCE_CSV,
    locations_csv_path: str | Path = "locations.csv",
) -> None:
    """Seed all initial database records."""
    clear_planning_history(session)
    seed_default_requirements(session)

    seed_locations_randomly = False
    if location_source == LOCATION_SOURCE_CSV:
        seed_locations_from_csv(session, locations_csv_path)
    elif location_source == LOCATION_SOURCE_RANDOM:
        seed_locations_randomly = True
    else:
        raise ValueError(
            f"Unknown location source {location_source!r}. "
            f"Use {LOCATION_SOURCE_CSV!r} or {LOCATION_SOURCE_RANDOM!r}."
        )

    seed_original_branch(session)
    seed_technicians(session, technicians_config)
    if seed_locations_randomly:
        seed_simulated_locations(session, locations_config)


__all__ = [
    "LOCATION_SOURCE_CSV",
    "LOCATION_SOURCE_RANDOM",
    "seed_database",
    "clear_planning_history",
]
