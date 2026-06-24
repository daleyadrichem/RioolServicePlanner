"""Database access helpers used by the ticket simulator."""

from __future__ import annotations

import random

from sqlalchemy import func
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.ticket_subjects import TicketSubject


class TicketRepository:
    """Small repository for simulator-specific database queries and inserts."""

    def __init__(self, session: Session) -> None:
        """Store the SQLAlchemy session used for all repository operations."""
        self.session = session

    def get_branch(self, branch_name: str) -> Branch:
        """Return a branch by name or raise when it does not exist."""
        branch = (
            self.session.query(Branch).filter(Branch.name == branch_name).one_or_none()
        )
        if branch is None:
            raise ValueError(f"Branch {branch_name!r} was not found")
        return branch

    def get_or_create_subject(self, subject_name: str) -> TicketSubject:
        """Return a ticket subject by name, creating a default one when missing."""
        subject = (
            self.session.query(TicketSubject)
            .filter(func.lower(TicketSubject.name) == subject_name.lower())
            .one_or_none()
        )

        if subject is None:
            subject = TicketSubject(
                name=subject_name,
                estimated_duration_minutes=60,
            )
            self.session.add(subject)
            self.session.flush()

        return subject

    def get_random_ticket_locations(
        self,
        *,
        branch: Branch,
        amount: int,
        rng: random.Random,
    ) -> list[Location]:
        """Return ``amount`` unique pre-seeded locations for simulated tickets."""
        query = (
            self.session.query(Location)
            .filter(Location.latitude.isnot(None))
            .filter(Location.longitude.isnot(None))
            .filter(Location.id != branch.location_id)
            .filter(
                Location.input_address.like(f"Simulated address near {branch.name}%")
            )
        )
        locations = query.all()

        if len(locations) < amount:
            raise ValueError(
                f"Scenario needs {amount} unique locations for branch {branch.name!r}, "
                f"but only {len(locations)} pre-seeded locations are available. "
                "Increase the count in locations_config.json and rerun initialize_database.py."
            )

        return rng.sample(locations, amount)

    def get_requirement(self, code: str) -> Requirement:
        """Return a requirement by code or raise when it does not exist."""
        requirement = (
            self.session.query(Requirement)
            .filter(func.lower(Requirement.code) == code.lower())
            .one_or_none()
        )
        if requirement is None:
            raise ValueError(f"Requirement code {code!r} was not found")
        return requirement
