"""Seed static domain data, technicians and reusable simulated locations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from riool_service.database_initializer.seed_data.technicians import seed_technicians
from riool_service.database_initializer.seed_data.requirements import (
    seed_default_requirements,
)
from riool_service.database_initializer.seed_data.locations import (
    seed_simulated_locations,
)
from riool_service.database_initializer.seed_data.branch import (
    seed_original_branch,
)


def seed_database(
    session: Session,
    *,
    technicians_config: dict[str, Any],
    locations_config: dict[str, Any],
) -> None:
    """Seed all initial database records."""
    seed_default_requirements(session)
    seed_original_branch(session)
    seed_technicians(session, technicians_config)
    seed_simulated_locations(session, locations_config)
