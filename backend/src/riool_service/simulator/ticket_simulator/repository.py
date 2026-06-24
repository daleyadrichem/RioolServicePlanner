"""Database access helpers used by the ticket simulator."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.ticket_subjects import TicketSubject

from .geocoding_types import ResolvedAddress
from .scenario_types import ScenarioConfig


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

    def get_or_create_location(
        self,
        *,
        scenario: ScenarioConfig,
        ticket_number: int,
        formatted_address: str,
        address: ResolvedAddress,
        latitude: float,
        longitude: float,
    ) -> Location:
        """Return a known location by formatted address, creating it when needed."""
        location = (
            self.session.query(Location)
            .filter(Location.formatted_address == formatted_address)
            .one_or_none()
        )

        if location is None:
            location = Location(
                input_address=(
                    f"Simulated address near {scenario['branch_name']} #{ticket_number}"
                ),
                formatted_address=formatted_address,
                street=address.street,
                house_number=address.house_number,
                city=address.city,
                country=address.country,
                latitude=latitude,
                longitude=longitude,
            )
            self.session.add(location)
            self.session.flush()

        return location

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
