"""Requirement seed data."""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from riool_service.database.models.requirement import Requirement

DEFAULT_REQUIREMENTS: Final[tuple[dict[str, str], ...]] = (
    {"code": "VEER", "name": "Trekveer"},
    {"code": "LADDER", "name": "Ladder"},
    {"code": "SUPPLIES", "name": "Benodigdheden"},
)


def seed_default_requirements(session: Session) -> None:
    """Insert or update the default requirements."""
    for requirement_data in DEFAULT_REQUIREMENTS:
        requirement = session.scalar(
            select(Requirement).where(Requirement.code == requirement_data["code"])
        )

        if requirement is None:
            session.add(Requirement(**requirement_data))
        else:
            requirement.name = requirement_data["name"]
