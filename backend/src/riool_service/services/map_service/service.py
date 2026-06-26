from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.planning_assignment import (
    PlanningAssignment,
    PlanningAssignmentStatus,
)
from riool_service.database.models.planning_run import PlanningRun, PlanningRunStatus
from riool_service.database.models.technician import Technician
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.tickets import Ticket, TicketStatus

VISIBLE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.COMPLETED,
}
TERMINAL_TICKET_STATUSES = {TicketStatus.COMPLETED, TicketStatus.CANCELLED}


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _format_location_address(location: Location | None) -> str:
    if location is None:
        return ""
    if location.street and location.house_number and location.city:
        return f"{location.street} {location.house_number}, {location.city}"
    value = location.formatted_address or location.input_address or location.city or ""
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return ", ".join(parts[:2]) if len(parts) > 2 else str(value).strip()


def _location_to_point(location: Location | None) -> dict[str, Any] | None:
    if location is None or location.latitude is None or location.longitude is None:
        return None
    return {
        "location_id": location.id,
        "address": _format_location_address(location),
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
    }


def _branch_to_hq(branch: Branch) -> dict[str, Any] | None:
    point = _location_to_point(branch.location)
    if point is None:
        return None
    return {
        "id": branch.id,
        "name": branch.name,
        **point,
    }


def _requirement_codes_from_ticket(ticket: Ticket) -> list[str]:
    return sorted(
        {
            link.requirement.code.upper()
            for link in ticket.ticket_requirements
            if link.requirement is not None and link.requirement.code is not None
        }
    )


def _requirement_codes_from_technician(technician: Technician) -> list[str]:
    return sorted(
        {
            link.requirement.code.upper()
            for link in technician.technician_requirements
            if link.requirement is not None and link.requirement.code is not None
        }
    )


def _ticket_to_map_marker(ticket: Ticket, assignment: PlanningAssignment | None = None) -> dict[str, Any] | None:
    point = _location_to_point(ticket.location)
    if point is None:
        return None
    active_assignment = assignment or _active_assignment(ticket)
    technician = active_assignment.technician if active_assignment is not None else None
    return {
        "id": ticket.id,
        "display_id": f"T-{ticket.id:03d}",
        "subject": ticket.subject.name if ticket.subject else "Onbekend",
        "urgency": _value(ticket.urgency),
        "status": _value(ticket.status),
        "description": ticket.description,
        "branch_id": ticket.branch_id,
        "branch_name": ticket.branch.name if ticket.branch else None,
        "deadline_at": ticket.deadline_at.isoformat() if ticket.deadline_at else None,
        "technician_id": technician.id if technician is not None else None,
        "technician_name": technician.name if technician is not None else None,
        "planned_start_at": active_assignment.planned_start_at.isoformat() if active_assignment is not None else None,
        "planned_end_at": active_assignment.planned_end_at.isoformat() if active_assignment is not None else None,
        "requirements": _requirement_codes_from_ticket(ticket),
        **point,
    }


def _active_assignment(ticket: Ticket) -> PlanningAssignment | None:
    assignments = [
        assignment
        for assignment in ticket.planning_assignments
        if assignment.status in VISIBLE_ASSIGNMENT_STATUSES
    ]
    if not assignments:
        return None
    return sorted(
        assignments,
        key=lambda assignment: assignment.planned_start_at,
        reverse=True,
    )[0]


def _technician_to_map_marker(
    technician: Technician,
    assignments: list[PlanningAssignment],
) -> dict[str, Any] | None:
    in_progress = next(
        (assignment for assignment in assignments if assignment.status == PlanningAssignmentStatus.IN_PROGRESS),
        None,
    )
    location = in_progress.ticket.location if in_progress is not None else technician.start_location
    point = _location_to_point(location)
    if point is None:
        return None
    requirements = _requirement_codes_from_technician(technician)
    return {
        "id": technician.id,
        "name": technician.name,
        "status": _value(technician.status),
        "branch_id": technician.branch_id,
        "requirements": requirements,
        "can_use_ladder": "LADDER" in requirements,
        "can_use_spring": "VEER" in requirements,
        "current_location_source": "ticket_in_progress" if in_progress is not None else "start_location",
        "current_ticket_id": in_progress.ticket_id if in_progress is not None else None,
        **point,
    }


def _latest_completed_planning_run(session: Session, branch_id: int) -> PlanningRun | None:
    return session.scalar(
        select(PlanningRun)
        .where(PlanningRun.branch_id == branch_id, PlanningRun.status == PlanningRunStatus.COMPLETED)
        .order_by(PlanningRun.completed_at.desc().nullslast(), PlanningRun.id.desc())
        .limit(1)
    )


def _available_assignment_dates(assignments: list[PlanningAssignment]) -> list[date]:
    return sorted(
        {
            assignment.planned_start_at.date()
            for assignment in assignments
            if assignment.planned_start_at is not None
        }
    )


def _coerce_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError as exc:
        raise ValueError("planned_date must be an ISO date, for example 2026-06-26") from exc


def _selected_planning_day(
    planned_date: str | date | datetime | None,
    available_dates: list[date],
    default_date: date | None,
) -> date | None:
    requested = _coerce_date(planned_date)
    if requested is not None:
        return requested
    if default_date in available_dates:
        return default_date
    return available_dates[0] if available_dates else default_date


def _load_branches(session: Session, branch_id: int | None) -> list[Branch]:
    statement = select(Branch).options(joinedload(Branch.location)).order_by(Branch.id)
    if branch_id is not None:
        statement = statement.where(Branch.id == int(branch_id))
    return list(session.scalars(statement).unique().all())


def _load_tickets(session: Session, branch_ids: list[int]) -> list[Ticket]:
    if not branch_ids:
        return []
    return list(
        session.scalars(
            select(Ticket)
            .options(
                joinedload(Ticket.subject),
                joinedload(Ticket.location),
                joinedload(Ticket.branch),
                joinedload(Ticket.ticket_requirements).joinedload(TicketRequirement.requirement),
                joinedload(Ticket.planning_assignments).joinedload(PlanningAssignment.technician),
            )
            .where(Ticket.branch_id.in_(branch_ids), Ticket.status.not_in(list(TERMINAL_TICKET_STATUSES)))
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        )
        .unique()
        .all()
    )


def _load_technicians(session: Session, branch_ids: list[int]) -> list[Technician]:
    if not branch_ids:
        return []
    return list(
        session.scalars(
            select(Technician)
            .options(
                joinedload(Technician.branch).joinedload(Branch.location),
                joinedload(Technician.home_location),
                joinedload(Technician.technician_requirements).joinedload(TechnicianRequirement.requirement),
            )
            .where(Technician.branch_id.in_(branch_ids))
            .order_by(Technician.name)
        )
        .unique()
        .all()
    )


def _load_latest_assignments_by_branch(session: Session, branch_ids: list[int]) -> tuple[dict[int, list[PlanningAssignment]], dict[int, PlanningRun]]:
    assignments_by_branch: dict[int, list[PlanningAssignment]] = {}
    runs_by_branch: dict[int, PlanningRun] = {}
    for branch_id in branch_ids:
        latest_run = _latest_completed_planning_run(session, branch_id)
        if latest_run is None:
            assignments_by_branch[branch_id] = []
            continue
        runs_by_branch[branch_id] = latest_run
        assignments_by_branch[branch_id] = list(
            session.scalars(
                select(PlanningAssignment)
                .options(
                    joinedload(PlanningAssignment.technician).joinedload(Technician.home_location),
                    joinedload(PlanningAssignment.technician).joinedload(Technician.branch).joinedload(Branch.location),
                    joinedload(PlanningAssignment.ticket).joinedload(Ticket.subject),
                    joinedload(PlanningAssignment.ticket).joinedload(Ticket.location),
                )
                .where(
                    PlanningAssignment.planning_run_id == latest_run.id,
                    PlanningAssignment.status.in_(list(VISIBLE_ASSIGNMENT_STATUSES)),
                )
                .order_by(PlanningAssignment.technician_id, PlanningAssignment.sequence_order)
            )
            .unique()
            .all()
        )
    return assignments_by_branch, runs_by_branch


def _route_for_technician(
    technician: Technician,
    assignments: list[PlanningAssignment],
) -> dict[str, Any] | None:
    ordered_assignments = sorted(assignments, key=lambda assignment: assignment.sequence_order)
    coordinates: list[list[float]] = []
    stops: list[dict[str, Any]] = []

    start = _location_to_point(technician.start_location)
    if start is not None:
        coordinates.append([start["latitude"], start["longitude"]])
        stops.append({"type": "start", "label": f"Start {technician.name}", **start})

    for assignment in ordered_assignments:
        if getattr(assignment, "requires_hq_pickup", False):
            hq_location = (
                assignment.technician.branch.location
                if assignment.technician and assignment.technician.branch
                else None
            )
            hq_point = _location_to_point(hq_location)
            if hq_point is not None:
                coordinates.append([hq_point["latitude"], hq_point["longitude"]])
                stops.append(
                    {
                        "type": "hq_pickup",
                        "label": "HQ pickup",
                        "ticket_id": assignment.ticket_id,
                        "assignment_id": assignment.id,
                        "sequence_order": assignment.sequence_order,
                        "travel_minutes_to_hq": int(
                            getattr(assignment, "estimated_travel_minutes_to_hq", 0) or 0
                        ),
                        "distance_km_to_hq": round(
                            float(getattr(assignment, "estimated_distance_km_to_hq", 0) or 0),
                            1,
                        ),
                        "travel_minutes_hq_to_ticket": int(
                            getattr(assignment, "estimated_travel_minutes_hq_to_ticket", 0) or 0
                        ),
                        "distance_km_hq_to_ticket": round(
                            float(
                                getattr(assignment, "estimated_distance_km_hq_to_ticket", 0)
                                or 0
                            ),
                            1,
                        ),
                        **hq_point,
                    }
                )

        point = _location_to_point(assignment.ticket.location)
        if point is None:
            continue
        coordinates.append([point["latitude"], point["longitude"]])
        stops.append(
            {
                "type": "ticket",
                "label": f"T-{assignment.ticket_id:03d}",
                "ticket_id": assignment.ticket_id,
                "assignment_id": assignment.id,
                "sequence_order": assignment.sequence_order,
                "planned_start_at": assignment.planned_start_at.isoformat() if assignment.planned_start_at else None,
                "planned_end_at": assignment.planned_end_at.isoformat() if assignment.planned_end_at else None,
                "requires_hq_pickup": bool(getattr(assignment, "requires_hq_pickup", False)),
                **point,
            }
        )

    end = _location_to_point(technician.end_location)
    if end is not None and coordinates:
        coordinates.append([end["latitude"], end["longitude"]])
        stops.append({"type": "end", "label": f"Einde {technician.name}", **end})

    if len(coordinates) < 2:
        return None

    return {
        "technician_id": technician.id,
        "technician_name": technician.name,
        "geometry_type": "straight_line",
        "coordinates": coordinates,
        "stops": stops,
        "ticket_ids": [assignment.ticket_id for assignment in ordered_assignments],
    }


def get_map_overview(session: Session, *, branch_id: int | None = None, planned_date: str | date | datetime | None = None) -> dict[str, Any]:
    """Return all data needed by the frontend map in one request.

    Route coordinates are intentionally straight-line polylines for the first UI
    version. Real road geometry can later replace `coordinates` without changing
    the frontend contract.
    """
    branches = _load_branches(session, branch_id)
    branch_ids = [branch.id for branch in branches]
    technicians = _load_technicians(session, branch_ids)
    assignments_by_branch, runs_by_branch = _load_latest_assignments_by_branch(session, branch_ids)
    all_assignments = [assignment for assignments in assignments_by_branch.values() for assignment in assignments]
    available_dates = _available_assignment_dates(all_assignments)
    default_date = next((run.planned_date for run in runs_by_branch.values() if run.planned_date in available_dates), None)
    selected_day = _selected_planning_day(planned_date, available_dates, default_date)
    assignments_by_branch = {
        current_branch_id: [
            assignment
            for assignment in assignments
            if selected_day is None
            or assignment.planned_start_at is None
            or assignment.planned_start_at.date() == selected_day
        ]
        for current_branch_id, assignments in assignments_by_branch.items()
    }

    assignments_by_technician: dict[int, list[PlanningAssignment]] = {}
    for assignments in assignments_by_branch.values():
        for assignment in assignments:
            assignments_by_technician.setdefault(assignment.technician_id, []).append(assignment)

    planned_ticket_markers = []
    for assignments in assignments_by_branch.values():
        for assignment in assignments:
            marker = _ticket_to_map_marker(assignment.ticket, assignment)
            if marker is not None:
                planned_ticket_markers.append(marker)

    mechanic_markers = [
        _technician_to_map_marker(technician, assignments_by_technician.get(technician.id, []))
        for technician in technicians
    ]
    mechanic_markers = [mechanic for mechanic in mechanic_markers if mechanic is not None]

    routes = [
        _route_for_technician(technician, assignments_by_technician.get(technician.id, []))
        for technician in technicians
    ]
    routes = [route for route in routes if route is not None]

    return {
        "hq": [_branch_to_hq(branch) for branch in branches if _branch_to_hq(branch) is not None],
        "tickets": planned_ticket_markers,
        "mechanics": mechanic_markers,
        "routes": routes,
        "planned_date": selected_day.isoformat() if selected_day else None,
        "available_dates": [value.isoformat() for value in available_dates],
        "meta": {
            "branch_id": branch_id,
            "planned_date": selected_day.isoformat() if selected_day else None,
            "available_dates": [value.isoformat() for value in available_dates],
            "route_geometry": "straight_line",
            "ticket_count": len(planned_ticket_markers),
            "mechanic_count": len(mechanic_markers),
            "route_count": len(routes),
        },
    }
