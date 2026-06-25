from __future__ import annotations

import random
from collections.abc import Iterable

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.ticket_subjects import TicketSubject
from riool_service.database.models.ticket_requirement import TicketRequirement

from riool_service.simulator.utils import haversine_km


def get_branch_by_name(session: Session, branch_name: str) -> Branch:
    branch = session.scalar(select(Branch).where(Branch.name == branch_name))
    if branch is None:
        raise ValueError(f"Branch {branch_name!r} was not found in the database")
    return branch


def get_or_create_subject(session: Session, name: str, duration_minutes: int = 60) -> TicketSubject:
    subject = session.scalar(select(TicketSubject).where(TicketSubject.name == name))
    if subject is not None:
        return subject

    subject = TicketSubject(name=name, estimated_duration_minutes=duration_minutes)
    session.add(subject)
    session.flush()
    return subject


def get_requirements_by_code(session: Session, codes: Iterable[str]) -> list[Requirement]:
    normalized_codes = {code.lower() for code in codes}
    if not normalized_codes:
        return []

    requirements = session.scalars(select(Requirement)).all()
    return [
        requirement
        for requirement in requirements
        if requirement.code.lower() in normalized_codes
        or requirement.name.lower() in normalized_codes
    ]


def choose_location_near_branch(
    session: Session,
    rng: random.Random,
    branch: Branch,
    radius_km: int | None = None,
) -> Location:
    """Choose a random existing location, preferably within the branch radius."""
    locations = session.scalars(select(Location)).all()
    if not locations:
        raise ValueError("No locations found in the database")

    branch_location = branch.location
    candidates = [location for location in locations if location.id != branch.location_id]

    if radius_km and branch_location and branch_location.has_coordinates():
        locations_in_radius = []
        for location in candidates:
            if not location.has_coordinates():
                continue
            distance = haversine_km(
                branch_location.latitude,
                branch_location.longitude,
                location.latitude,
                location.longitude,
            )
            if distance <= radius_km:
                locations_in_radius.append(location)
        candidates = locations_in_radius or candidates

    return rng.choice(candidates or locations)


def ticket_requirement_schema_supports_single_parent(session: Session) -> bool:
    """Return whether requirement links can point to ticket OR simulation ticket.

    The original outline made both ``ticket_id`` and ``simulation_ticket_id``
    non-nullable. That would force every requirement row to point to both tables
    at once. The patched ORM model below allows either parent, but this helper
    also protects existing databases that still have the old NOT NULL schema.
    """
    columns = inspect(session.bind).get_columns(TicketRequirement.__tablename__)
    nullable_by_name = {column["name"]: bool(column.get("nullable")) for column in columns}
    return nullable_by_name.get("ticket_id", True) or nullable_by_name.get(
        "simulation_ticket_id", True
    )


def add_requirement_links(
    session: Session,
    *,
    requirement_codes: Iterable[str],
    ticket_id: int | None = None,
    simulation_ticket_id: int | None = None,
) -> int:
    if ticket_id is None and simulation_ticket_id is None:
        raise ValueError("ticket_id or simulation_ticket_id is required")

    if not ticket_requirement_schema_supports_single_parent(session):
        return 0

    count = 0
    for requirement in get_requirements_by_code(session, requirement_codes):
        session.add(
            TicketRequirement(
                ticket_id=ticket_id,
                simulation_ticket_id=simulation_ticket_id,
                requirement_id=requirement.id,
            )
        )
        count += 1
    return count
