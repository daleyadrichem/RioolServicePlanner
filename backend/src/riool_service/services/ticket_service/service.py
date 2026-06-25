from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.models.base import Base
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.planning_assignment import (
    PlanningAssignment,
    PlanningAssignmentStatus,
)
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.technician import Technician
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.ticket_subjects import TicketSubject
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.database.db_utils import get_engine
from riool_service.simulator.db_helpers import add_requirement_links, get_branch_by_name, get_or_create_subject
from riool_service.simulator.utils import deadline_for

DEFAULT_BRANCH_NAME = "Branch Den Bosch"
TERMINAL_STATUSES = {TicketStatus.COMPLETED, TicketStatus.CANCELLED}
ACTIVE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.COMPLETED,
}


class TicketNotFoundError(ValueError):
    """Raised when a requested ticket does not exist."""


def ensure_ticket_tables() -> None:
    """Create tables that may not exist yet in older local databases."""
    Base.metadata.create_all(get_engine())


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def normalize_urgency(value: str | TicketUrgency | None) -> TicketUrgency:
    if isinstance(value, TicketUrgency):
        return value
    normalized = str(value or "").strip().upper()
    aliases = {
        "URGENT": TicketUrgency.URGENT,
        "SPOED": TicketUrgency.URGENT,
        "HIGH": TicketUrgency.URGENT,
        "MEDIUM": TicketUrgency.MEDIUM,
        "MID": TicketUrgency.MEDIUM,
        "NORMAL": TicketUrgency.MEDIUM,
        "NORMAAL": TicketUrgency.MEDIUM,
        "LOW": TicketUrgency.LOW,
        "LAAG": TicketUrgency.LOW,
    }
    if normalized not in aliases:
        raise ValueError("urgency must be one of urgent, medium, or low")
    return aliases[normalized]


def normalize_status(value: str | TicketStatus | None) -> TicketStatus:
    if isinstance(value, TicketStatus):
        return value
    normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "OPEN": TicketStatus.OPEN,
        "NIEUW": TicketStatus.OPEN,
        "PLANNED": TicketStatus.PLANNED,
        "GEPLAND": TicketStatus.PLANNED,
        "IN_PROGRESS": TicketStatus.IN_PROGRESS,
        "ONDERWEG": TicketStatus.IN_PROGRESS,
        "BEZIG": TicketStatus.IN_PROGRESS,
        "COMPLETED": TicketStatus.COMPLETED,
        "DONE": TicketStatus.COMPLETED,
        "FINISHED": TicketStatus.COMPLETED,
        "AFGEROND": TicketStatus.COMPLETED,
        "CANCELLED": TicketStatus.CANCELLED,
        "CANCELED": TicketStatus.CANCELLED,
        "GEANNULEERD": TicketStatus.CANCELLED,
    }
    if normalized not in aliases:
        raise ValueError("status must be one of open, planned, in_progress, completed, or cancelled")
    return aliases[normalized]


def _requirement_codes(ticket: Ticket) -> set[str]:
    return {
        link.requirement.code.upper()
        for link in ticket.ticket_requirements
        if link.requirement is not None and link.requirement.code is not None
    }


def _format_location_address(location: Location | None) -> str:
    if location is None:
        return ""
    if location.street and location.house_number and location.city:
        return f"{location.street} {location.house_number}, {location.city}"
    value = location.formatted_address or location.input_address or location.city or ""
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return ", ".join(parts[:2]) if len(parts) > 2 else str(value).strip()


def _active_assignment(ticket: Ticket) -> PlanningAssignment | None:
    active = [
        assignment
        for assignment in ticket.planning_assignments
        if assignment.status in ACTIVE_ASSIGNMENT_STATUSES
    ]
    if not active:
        return None
    return sorted(active, key=lambda item: item.planned_start_at or datetime.min, reverse=True)[0]


def _ticket_to_dict(ticket: Ticket) -> dict[str, Any]:
    requirements = _requirement_codes(ticket)
    assignment = _active_assignment(ticket)
    technician = assignment.technician if assignment is not None else None
    is_terminal = ticket.status in TERMINAL_STATUSES

    return {
        "id": f"T-{ticket.id:03d}",
        "database_id": ticket.id,
        "subject": ticket.subject.name if ticket.subject else "Onbekend",
        "subject_id": ticket.subject_id,
        "address": _format_location_address(ticket.location),
        "location_id": ticket.location_id,
        "branch_id": ticket.branch_id,
        "branch_name": ticket.branch.name if ticket.branch else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "deadline_at": ticket.deadline_at.isoformat() if ticket.deadline_at else None,
        "urgency": _value(ticket.urgency),
        "status": _value(ticket.status),
        "description": ticket.description,
        "requires_ladder": "LADDER" in requirements,
        "requires_spring": "VEER" in requirements,
        "requirements": sorted(requirements),
        "technician_id": technician.id if technician is not None else None,
        "technician_name": technician.name if technician is not None else None,
        "planned_start_at": assignment.planned_start_at.isoformat() if assignment is not None else None,
        "planned_end_at": assignment.planned_end_at.isoformat() if assignment is not None else None,
        "is_unplanned": assignment is None and not is_terminal,
        "is_open": not is_terminal,
        "is_urgent_open": ticket.urgency == TicketUrgency.URGENT and not is_terminal,
    }


def _ticket_options():
    return (
        joinedload(Ticket.subject),
        joinedload(Ticket.location),
        joinedload(Ticket.branch),
        joinedload(Ticket.ticket_requirements).joinedload(TicketRequirement.requirement),
        joinedload(Ticket.planning_assignments).joinedload(PlanningAssignment.technician),
    )


def _base_query():
    return select(Ticket).options(*_ticket_options()).order_by(Ticket.created_at.desc(), Ticket.id.desc())


def _apply_urgency_filter(statement, urgency: str | None):
    if not urgency or str(urgency).lower() in {"all", "alle"}:
        return statement
    return statement.where(Ticket.urgency == normalize_urgency(urgency))


def _apply_status_filter(statement, status: str | None):
    normalized = str(status or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"", "all", "alle"}:
        return statement
    if normalized == "open":
        return statement.where(Ticket.status.not_in(list(TERMINAL_STATUSES)))
    if normalized == "finished":
        return statement.where(Ticket.status == TicketStatus.COMPLETED)
    if normalized == "urgent_open":
        return statement.where(
            Ticket.urgency == TicketUrgency.URGENT,
            Ticket.status.not_in(list(TERMINAL_STATUSES)),
        )
    if normalized == "unplanned":
        active_assignment_exists = (
            select(PlanningAssignment.id)
            .where(
                PlanningAssignment.ticket_id == Ticket.id,
                PlanningAssignment.status.in_(list(ACTIVE_ASSIGNMENT_STATUSES)),
            )
            .exists()
        )
        return statement.where(Ticket.status.not_in(list(TERMINAL_STATUSES)), ~active_assignment_exists)
    return statement.where(Ticket.status == normalize_status(normalized))


def list_tickets(session: Session, *, urgency: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    statement = _apply_status_filter(_apply_urgency_filter(_base_query(), urgency), status)
    tickets = session.scalars(statement).unique().all()
    return [_ticket_to_dict(ticket) for ticket in tickets]


def get_ticket(session: Session, ticket_id: int) -> dict[str, Any]:
    ticket = session.scalars(select(Ticket).options(*_ticket_options()).where(Ticket.id == ticket_id)).unique().first()
    if ticket is None:
        raise TicketNotFoundError(f"Ticket {ticket_id} was not found")
    return _ticket_to_dict(ticket)


def _count_with_status(session: Session, status: str) -> int:
    statement = _apply_status_filter(select(func.count(Ticket.id)), status)
    return int(session.scalar(statement) or 0)


def get_statistics(session: Session) -> dict[str, int]:
    return {
        "total": int(session.scalar(select(func.count(Ticket.id))) or 0),
        "open": _count_with_status(session, "open"),
        "urgent_open": _count_with_status(session, "urgent_open"),
        "unplanned": _count_with_status(session, "unplanned"),
        "finished": _count_with_status(session, "finished"),
    }


def _requirement_codes_from_payload(payload: dict[str, Any]) -> list[str]:
    codes = {str(code).upper() for code in payload.get("requirements") or [] if str(code).strip()}
    if payload.get("requires_ladder"):
        codes.add("LADDER")
    if payload.get("requires_spring"):
        codes.add("VEER")
    return sorted(codes)


def _replace_ticket_requirements(session: Session, *, ticket_id: int, requirement_codes: list[str]) -> None:
    for link in session.scalars(select(TicketRequirement).where(TicketRequirement.ticket_id == ticket_id)).all():
        session.delete(link)
    session.flush()
    add_requirement_links(session, requirement_codes=requirement_codes, ticket_id=ticket_id)


def list_branches(session: Session) -> list[dict[str, Any]]:
    branches = session.scalars(select(Branch).options(joinedload(Branch.location)).order_by(Branch.name)).unique().all()
    return [
        {
            "id": branch.id,
            "name": branch.name,
            "address": _format_location_address(branch.location),
            "location_id": branch.location_id,
            "latitude": float(branch.location.latitude) if branch.location and branch.location.latitude is not None else None,
            "longitude": float(branch.location.longitude) if branch.location and branch.location.longitude is not None else None,
        }
        for branch in branches
    ]


def list_technicians(session: Session) -> list[dict[str, Any]]:
    technicians = session.scalars(
        select(Technician)
        .options(
            joinedload(Technician.branch),
            joinedload(Technician.technician_requirements).joinedload(TechnicianRequirement.requirement),
        )
        .order_by(Technician.name)
    ).unique().all()
    result = []
    for technician in technicians:
        requirement_codes = sorted(
            link.requirement.code
            for link in technician.technician_requirements
            if link.requirement is not None and link.requirement.code is not None
        )
        result.append({
            "id": technician.id,
            "name": technician.name,
            "branch_id": technician.branch_id,
            "branch_name": technician.branch.name if technician.branch else None,
            "status": _value(technician.status),
            "requirements": requirement_codes,
            "can_use_ladder": "LADDER" in {code.upper() for code in requirement_codes},
            "can_use_spring": "VEER" in {code.upper() for code in requirement_codes},
        })
    return result


def _fallback_branch(session: Session) -> Branch:
    branch = session.scalar(select(Branch).order_by(Branch.id))
    if branch is None:
        raise ValueError("No branches found in database")
    return branch


def _branch_from_payload(session: Session, payload: dict[str, Any]) -> Branch:
    branch_id = payload.get("branch_id")
    if branch_id not in (None, ""):
        branch = session.get(Branch, int(branch_id))
        if branch is None:
            raise ValueError(f"Branch with id {branch_id!r} was not found in database")
        return branch

    branch_name = str(payload.get("branch_name") or "").strip()
    if branch_name:
        try:
            return get_branch_by_name(session, branch_name)
        except ValueError:
            # Allow a user-facing city name such as "Den Bosch" to match the seeded
            # branch name "Branch Den Bosch" without hardcoding that relation in the UI.
            contains_match = session.scalar(
                select(Branch).where(func.lower(Branch.name).contains(branch_name.lower())).order_by(Branch.id)
            )
            if contains_match is not None:
                return contains_match
            raise

    try:
        return get_branch_by_name(session, DEFAULT_BRANCH_NAME)
    except ValueError:
        return _fallback_branch(session)


def _default_branch(session: Session) -> Branch:
    return _branch_from_payload(session, {})


def _get_or_create_location(session: Session, payload: dict[str, Any]) -> Location:
    location_id = payload.get("location_id")
    if location_id:
        location = session.get(Location, int(location_id))
        if location is None:
            raise ValueError(f"Location {location_id} was not found")
        return location

    address = str(payload.get("address") or "").strip()
    city = str(payload.get("city") or DEFAULT_BRANCH_NAME).strip()
    if not address:
        raise ValueError("address is required")

    if "," in address:
        first_part, city_part, *_ = [part.strip() for part in address.split(",")]
        address = first_part
        city = city_part or city

    parts = address.rsplit(" ", 1)
    if len(parts) != 2:
        raise ValueError("address must contain street and house number")
    street, house_number = parts[0].strip(), parts[1].strip()
    formatted_address = f"{street} {house_number}, {city}"

    existing = session.scalar(
        select(Location).where(
            (func.lower(Location.formatted_address) == formatted_address.lower())
            | (func.lower(Location.input_address) == formatted_address.lower())
        )
    )
    if existing is not None:
        return existing

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if latitude is None or longitude is None:
        # Manual ticket creation with strict geocoding will be handled in the next step.
        # For now, keep this endpoint fast and deterministic by using branch coordinates
        # when a caller does not already provide validated coordinates.
        branch_location = _default_branch(session).location
        latitude = branch_location.latitude
        longitude = branch_location.longitude

    location = Location(
        input_address=formatted_address,
        formatted_address=formatted_address,
        street=street,
        house_number=house_number,
        city=city,
        latitude=float(latitude),
        longitude=float(longitude),
    )
    session.add(location)
    session.flush()
    return location


def create_ticket(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    branch = _branch_from_payload(session, payload)
    subject_name = str(payload.get("subject") or "").strip()
    if not subject_name:
        raise ValueError("subject is required")
    subject = get_or_create_subject(session, subject_name)
    location = _get_or_create_location(session, payload)
    created_at = datetime.now()
    urgency = normalize_urgency(payload.get("urgency", "medium"))

    ticket = Ticket(
        branch_id=branch.id,
        location_id=location.id,
        subject_id=subject.id,
        description=payload.get("description"),
        urgency=urgency,
        status=TicketStatus.OPEN,
        created_at=created_at,
        deadline_at=deadline_for(created_at, urgency),
    )
    session.add(ticket)
    session.flush()
    _replace_ticket_requirements(session, ticket_id=ticket.id, requirement_codes=_requirement_codes_from_payload(payload))
    session.flush()
    return get_ticket(session, ticket.id)


def update_ticket(session: Session, ticket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(f"Ticket {ticket_id} was not found")

    if "subject" in payload and payload.get("subject"):
        ticket.subject_id = get_or_create_subject(session, str(payload["subject"]).strip()).id
    if "description" in payload:
        ticket.description = payload.get("description")
    if "urgency" in payload:
        ticket.urgency = normalize_urgency(payload.get("urgency"))
        ticket.deadline_at = deadline_for(ticket.created_at, ticket.urgency)
    if "status" in payload:
        ticket.status = normalize_status(payload.get("status"))
        if ticket.status == TicketStatus.COMPLETED and ticket.completed_at is None:
            ticket.completed_at = datetime.now()
        if ticket.status != TicketStatus.COMPLETED:
            ticket.completed_at = None
    if "address" in payload or "location_id" in payload:
        ticket.location_id = _get_or_create_location(session, payload).id
    if {"requirements", "requires_ladder", "requires_spring"} & set(payload):
        _replace_ticket_requirements(session, ticket_id=ticket.id, requirement_codes=_requirement_codes_from_payload(payload))

    session.flush()
    return get_ticket(session, ticket.id)


def delete_ticket(session: Session, ticket_id: int) -> dict[str, Any]:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(f"Ticket {ticket_id} was not found")
    session.delete(ticket)
    session.flush()
    return {"deleted": True, "id": ticket_id}
