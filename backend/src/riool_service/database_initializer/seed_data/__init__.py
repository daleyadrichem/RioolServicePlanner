"""Seed static domain data, technicians and reusable simulated locations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from riool_service.database.models.planning_assignment import PlanningAssignment
from riool_service.database.models.planning_run import PlanningRun

from .branch import seed_original_branch
from .locations import seed_simulated_locations
from .requirements import seed_default_requirements
from .technicians import seed_technicians


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
) -> None:
    """Seed all initial database records."""
    clear_planning_history(session)
    seed_default_requirements(session)
    seed_original_branch(session)
    seed_technicians(session, technicians_config)
    seed_simulated_locations(session, locations_config)


__all__ = ["seed_database", "clear_planning_history"]
