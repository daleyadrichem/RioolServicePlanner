"""Seed static domain data, technicians and reusable simulated locations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .branch import seed_original_branch
from .locations import seed_simulated_locations
from .requirements import seed_default_requirements
from .technicians import seed_technicians


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


__all__ = ["seed_database"]
