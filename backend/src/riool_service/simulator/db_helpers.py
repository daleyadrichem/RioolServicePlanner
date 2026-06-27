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
from riool_service.database.models.technician import Technician

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


def location_address_key(location: Location) -> str:
    """Return a stable, normalized key for detecting duplicate addresses."""
    structured_parts = [
        str(location.street or "").strip(),
        str(location.house_number or "").strip(),
        str(location.city or "").strip(),
    ]
    if any(structured_parts):
        raw_key = "|".join(structured_parts)
    else:
        raw_key = str(location.formatted_address or location.input_address or "").strip()

    normalized = " ".join(raw_key.lower().split())
    return normalized or f"location-id:{location.id}"


def simulator_reserved_location_ids(session: Session) -> set[int]:
    """Return location ids that simulator tickets must not use."""
    reserved_ids = {
        location_id
        for location_id in session.scalars(select(Branch.location_id)).all()
        if location_id is not None
    }
    reserved_ids.update(
        location_id
        for location_id in session.scalars(select(Technician.home_location_id)).all()
        if location_id is not None
    )
    return reserved_ids


def simulator_reserved_address_keys(session: Session) -> set[str]:
    """Return HQ/mechanic-home address keys that simulator tickets must not use.

    This blocks both exact reserved location rows and duplicate location rows with
    the same address.
    """
    reserved_ids = simulator_reserved_location_ids(session)
    if not reserved_ids:
        return set()

    reserved_locations = session.scalars(
        select(Location).where(Location.id.in_(reserved_ids))
    ).all()
    return {location_address_key(location) for location in reserved_locations}


def choose_location_near_branch(
    session: Session,
    rng: random.Random,
    branch: Branch,
    radius_km: int | None = None,
    excluded_location_ids: set[int] | None = None,
    excluded_address_keys: set[str] | None = None,
    used_address_keys: set[str] | None = None,
) -> Location:
    """Choose a random eligible location, preferably within the branch radius.

    ``excluded_location_ids`` and ``excluded_address_keys`` keep simulator tickets
    away from HQ and technician home locations. ``used_address_keys`` prevents
    two generated tickets in one scenario run from using the same address.
    """
    locations = session.scalars(select(Location)).all()
    if not locations:
        raise ValueError("No locations found in the database")

    blocked_ids = set(excluded_location_ids or set())
    # Keep the current branch/HQ location blocked even if a caller forgets to
    # include it in excluded_location_ids.
    blocked_ids.add(branch.location_id)

    blocked_address_keys = set(excluded_address_keys or set())
    if branch.location is not None:
        blocked_address_keys.add(location_address_key(branch.location))

    used_keys = used_address_keys if used_address_keys is not None else set()
    candidates = [
        location
        for location in locations
        if location.id not in blocked_ids
        and location_address_key(location) not in blocked_address_keys
        and location_address_key(location) not in used_keys
    ]

    if radius_km and branch.location and branch.location.has_coordinates():
        locations_in_radius = []
        for location in candidates:
            if not location.has_coordinates():
                continue
            distance = haversine_km(
                branch.location.latitude,
                branch.location.longitude,
                location.latitude,
                location.longitude,
            )
            if distance <= radius_km:
                locations_in_radius.append(location)
        candidates = locations_in_radius or candidates

    if not candidates:
        raise ValueError(
            "Not enough unique eligible customer locations to generate tickets. "
            "Mechanic home addresses, HQ addresses, and duplicate addresses are excluded."
        )

    selected = rng.choice(candidates)
    used_keys.add(location_address_key(selected))
    return selected


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
