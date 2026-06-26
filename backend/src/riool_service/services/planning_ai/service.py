from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.db_utils import get_engine
from riool_service.database_initializer.database import create_schema
from riool_service.database.models.planning_assignment import (
    PlanningAssignment,
    PlanningAssignmentSource,
    PlanningAssignmentStatus,
)
from riool_service.database.models.planning_run import (
    PlanningRun,
    PlanningRunStatus,
    PlanningRunTrigger,
)
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.route_cache import RouteCache, RouteProvider
from riool_service.database.models.technician import Technician
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.services.planning_ai.models import (
    PlannedBreak,
    PlannedStop,
    PlannedTravel,
    PlannedRequirementPickup,
    PlanningConfig,
    PlanningSolution,
)
from riool_service.services.planning_ai.optimizer import (
    InitialRouteOptimizer,
    SLA_MISS_PENALTY,
    UNPLANNED_TICKET_PENALTY,
    UNPLANNED_URGENCY_TIEBREAKER,
    OVERTIME_PENALTY_PER_MINUTE,
)
from riool_service.services.planning_ai.routing import get_planning_route_matrix
from riool_service.services.planning_ai.selection import load_available_technicians, load_candidate_tickets


SUPPLY_REQUIREMENT_CODES = {"SUPPLIES"}


class PlanningAiError(ValueError):
    pass


def ensure_planning_ai_tables() -> None:
    create_schema(get_engine())




TERMINAL_TICKET_STATUSES = {TicketStatus.COMPLETED, TicketStatus.CANCELLED}
VISIBLE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.IN_PROGRESS,
}


def get_planning_overview(session: Session, *, branch_id: int | None = None, planned_date: str | date | datetime | None = None) -> dict[str, Any]:
    """Return the current planner board built from persisted assignments.

    The frontend uses this to decide whether to show "Start planning" or
    "Herplannen" and to render the actual tickets assigned to each mechanic.
    "Urgent open" intentionally means urgent tickets with ticket status OPEN;
    planned-but-not-finished tickets are no longer counted as open here.
    """
    branch = _overview_branch(session, branch_id)
    latest_run = _latest_completed_planning_run(session, branch.id)
    technicians = _overview_technicians(session, branch.id)
    all_assignments = _overview_assignments(session, latest_run.id if latest_run else None)
    available_dates = _available_assignment_dates(all_assignments)
    selected_day = _selected_planning_day(planned_date, available_dates, latest_run.planned_date if latest_run else None)
    assignments = [
        assignment
        for assignment in all_assignments
        if selected_day is None
        or assignment.planned_start_at is None
        or assignment.planned_start_at.date() == selected_day
    ]

    assignments_by_technician: dict[int, list[PlanningAssignment]] = {}
    for assignment in assignments:
        assignments_by_technician.setdefault(assignment.technician_id, []).append(assignment)

    route_lookup = _route_cache_lookup(session, technicians, assignments_by_technician)

    columns = []
    selected_day_anchor = _day_anchor(selected_day, latest_run.planned_date if latest_run else None)
    for technician in technicians:
        technician_assignments = assignments_by_technician.get(technician.id, [])
        timeline_start = _technician_day_start(technician, selected_day_anchor)
        timeline_end = _technician_day_end(technician, selected_day_anchor)
        columns.append(
            {
                "technician": _technician_to_overview_dict(technician),
                "planning_date": selected_day.isoformat() if selected_day else None,
                "timeline_start_at": timeline_start.isoformat() if timeline_start else None,
                "timeline_end_at": timeline_end.isoformat() if timeline_end else None,
                "items": _assignments_to_timeline_items(
                    technician,
                    technician_assignments,
                    planned_date=selected_day_anchor,
                    route_lookup=route_lookup,
                ),
            }
        )

    assigned_ticket_ids = {assignment.ticket_id for assignment in assignments}
    horizon_days = 1
    total_open = _count_open_tickets(session, branch.id)
    urgent_open = _count_urgent_open_tickets(session, branch.id)
    total_minutes = _total_workday_minutes(technicians) * horizon_days
    used_minutes = sum(
        int(assignment.estimated_duration_minutes or 0) + int(assignment.estimated_travel_minutes_before or 0)
        for assignment in assignments
    )
    used_minutes += sum(
        5
        for assignment in assignments
        if getattr(assignment, "requires_hq_pickup", False)
    )
    if latest_run is not None:
        used_minutes += len(technicians) * 45
    travel_minutes = sum(int(assignment.estimated_travel_minutes_before or 0) for assignment in assignments)
    kilometers = round(sum(float(assignment.estimated_distance_km_before or 0) for assignment in assignments), 1)

    return {
        "has_plan": latest_run is not None,
        "planning_run_id": latest_run.id if latest_run else None,
        "planned_date": selected_day.isoformat() if selected_day else (latest_run.planned_date.isoformat() if latest_run and latest_run.planned_date else None),
        "available_dates": [value.isoformat() for value in available_dates],
        "stats": {
            "total_today": total_open,
            "planned": len(assigned_ticket_ids),
            "urgent_open": urgent_open,
            "kilometers": kilometers,
            "travel_minutes": travel_minutes,
            "free_minutes": max(0, total_minutes - used_minutes),
        },
        "columns": columns,
    }



def _available_assignment_dates(assignments: list[PlanningAssignment]) -> list[date]:
    return sorted(
        {assignment.planned_start_at.date() for assignment in assignments if assignment.planned_start_at is not None}
    )


def _selected_planning_day(
    requested: str | date | datetime | None,
    available_dates: list[date],
    fallback: datetime | None,
) -> date | None:
    if requested is not None:
        if isinstance(requested, datetime):
            return requested.date()
        if isinstance(requested, date):
            return requested
        parsed = datetime.fromisoformat(str(requested).replace("Z", "+00:00"))
        return parsed.date()
    if available_dates:
        return available_dates[0]
    return fallback.date() if fallback is not None else None


def _day_anchor(selected_day: date | None, fallback: datetime | None) -> datetime | None:
    if selected_day is None:
        return fallback
    return datetime.combine(selected_day, time.min, tzinfo=fallback.tzinfo if fallback else None)

def run_replanning(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a new plan and mark previous active assignments as moved.

    Existing assignments are kept for history, but no longer appear as active on
    tickets once the new plan is generated.
    """
    _move_existing_active_assignments(session, int(payload.get("branch_id") or 1))
    result = run_initial_planning(session, payload)
    result["overview"] = get_planning_overview(session, branch_id=int(payload.get("branch_id") or 1))
    return result


def _overview_branch(session: Session, branch_id: int | None) -> Branch:
    if branch_id:
        branch = session.get(Branch, int(branch_id))
        if branch is not None:
            return branch
    branch = session.scalar(select(Branch).order_by(Branch.id))
    if branch is None:
        raise PlanningAiError("No branches found in database")
    return branch


def _latest_completed_planning_run(session: Session, branch_id: int) -> PlanningRun | None:
    return session.scalar(
        select(PlanningRun)
        .where(PlanningRun.branch_id == branch_id, PlanningRun.status == PlanningRunStatus.COMPLETED)
        .order_by(PlanningRun.completed_at.desc().nullslast(), PlanningRun.id.desc())
        .limit(1)
    )


def _overview_technicians(session: Session, branch_id: int) -> list[Technician]:
    return list(
        session.scalars(
            select(Technician)
            .options(
                joinedload(Technician.branch).joinedload(Branch.location),
                joinedload(Technician.home_location),
                joinedload(Technician.technician_requirements).joinedload(TechnicianRequirement.requirement),
            )
            .where(Technician.branch_id == branch_id)
            .order_by(Technician.name)
        ).unique().all()
    )


def _overview_assignments(session: Session, planning_run_id: int | None) -> list[PlanningAssignment]:
    if planning_run_id is None:
        return []
    return list(
        session.scalars(
            select(PlanningAssignment)
            .options(
                joinedload(PlanningAssignment.technician),
                joinedload(PlanningAssignment.ticket).joinedload(Ticket.subject),
                joinedload(PlanningAssignment.ticket).joinedload(Ticket.location),
                joinedload(PlanningAssignment.ticket)
                .joinedload(Ticket.ticket_requirements)
                .joinedload(TicketRequirement.requirement),
            )
            .where(
                PlanningAssignment.planning_run_id == planning_run_id,
                PlanningAssignment.status.in_(list(VISIBLE_ASSIGNMENT_STATUSES)),
            )
            .order_by(PlanningAssignment.technician_id, PlanningAssignment.sequence_order)
        ).unique().all()
    )


def _route_cache_lookup(
    session: Session,
    technicians: list[Technician],
    assignments_by_technician: dict[int, list[PlanningAssignment]],
) -> dict[tuple[int, int], tuple[int, float]]:
    pairs: set[tuple[int, int]] = set()
    for technician in technicians:
        assignments = assignments_by_technician.get(technician.id, [])
        previous_location_id = (
            technician.start_location.id if technician.start_location is not None else None
        )
        hq_location_id = technician.branch.location_id if technician.branch else None
        for assignment in assignments:
            ticket_location_id = assignment.ticket.location_id
            requirement_codes = _supply_requirement_codes_from_ticket(assignment.ticket)
            if (
                requirement_codes
                and previous_location_id is not None
                and hq_location_id is not None
                and ticket_location_id is not None
            ):
                pairs.add((previous_location_id, hq_location_id))
                pairs.add((hq_location_id, ticket_location_id))
            previous_location_id = ticket_location_id

    if not pairs:
        return {}

    rows = session.scalars(
        select(RouteCache).where(
            RouteCache.provider == RouteProvider.OSRM,
            RouteCache.from_location_id.in_({pair[0] for pair in pairs}),
            RouteCache.to_location_id.in_({pair[1] for pair in pairs}),
        )
    ).all()
    lookup = {
        (row.from_location_id, row.to_location_id): (
            int(row.travel_minutes),
            float(row.distance_km),
        )
        for row in rows
    }
    return {pair: lookup[pair] for pair in pairs if pair in lookup}


def _format_location_address(location: Location | None) -> str:
    if location is None:
        return ""
    if location.street and location.house_number and location.city:
        return f"{location.street} {location.house_number}, {location.city}"
    value = location.formatted_address or location.input_address or location.city or ""
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return ", ".join(parts[:2]) if len(parts) > 2 else str(value).strip()


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _requirement_codes_from_ticket(ticket: Ticket) -> list[str]:
    return sorted(_requirement_code_set_from_ticket(ticket))


def _requirement_code_set_from_ticket(ticket: Ticket) -> set[str]:
    return {
        link.requirement.code.upper()
        for link in ticket.ticket_requirements
        if link.requirement is not None and link.requirement.code is not None
    }


def _skill_requirement_codes_from_ticket(ticket: Ticket) -> list[str]:
    return sorted(code for code in _requirement_code_set_from_ticket(ticket) if code not in SUPPLY_REQUIREMENT_CODES)


def _supply_requirement_codes_from_ticket(ticket: Ticket) -> list[str]:
    return sorted(code for code in _requirement_code_set_from_ticket(ticket) if code in SUPPLY_REQUIREMENT_CODES)


def _technician_to_overview_dict(technician: Technician) -> dict[str, Any]:
    codes = sorted(
        link.requirement.code.upper()
        for link in technician.technician_requirements
        if link.requirement is not None and link.requirement.code is not None
    )
    return {
        "id": technician.id,
        "name": technician.name,
        "branch_id": technician.branch_id,
        "requirements": codes,
        "can_use_ladder": "LADDER" in codes,
        "can_use_spring": "VEER" in codes,
    }


def _assignment_to_planning_item(assignment: PlanningAssignment) -> dict[str, Any]:
    ticket = assignment.ticket
    codes = _requirement_codes_from_ticket(ticket)
    return {
        "id": assignment.id,
        "ticket_id": ticket.id,
        "ticket_display_id": f"T-{ticket.id:03d}",
        "title": ticket.subject.name if ticket.subject else "Onbekend",
        "subject": ticket.subject.name if ticket.subject else "Onbekend",
        "address": _format_location_address(ticket.location),
        "start": assignment.planned_start_at.strftime("%H:%M") if assignment.planned_start_at else "",
        "end": assignment.planned_end_at.strftime("%H:%M") if assignment.planned_end_at else "",
        "planned_start_at": assignment.planned_start_at.isoformat() if assignment.planned_start_at else None,
        "planned_end_at": assignment.planned_end_at.isoformat() if assignment.planned_end_at else None,
        "duration_minutes": assignment.estimated_duration_minutes,
        "travel_minutes_before": assignment.estimated_travel_minutes_before,
        "distance_km_before": round(float(assignment.estimated_distance_km_before or 0), 1),
        "requires_hq_pickup": bool(getattr(assignment, "requires_hq_pickup", False)),
        "hq_location_id": getattr(assignment, "hq_location_id", None),
        "travel_minutes_to_hq": int(getattr(assignment, "estimated_travel_minutes_to_hq", 0) or 0),
        "distance_km_to_hq": round(float(getattr(assignment, "estimated_distance_km_to_hq", 0) or 0), 1),
        "travel_minutes_hq_to_ticket": int(getattr(assignment, "estimated_travel_minutes_hq_to_ticket", 0) or 0),
        "distance_km_hq_to_ticket": round(float(getattr(assignment, "estimated_distance_km_hq_to_ticket", 0) or 0), 1),
        "urgency": _value(ticket.urgency),
        "status": _value(ticket.status),
        "assignment_status": _value(assignment.status),
        "description": ticket.description,
        "requires_ladder": "LADDER" in codes,
        "requires_spring": "VEER" in codes,
        "requires_supplies": "SUPPLIES" in codes,
        "requirements": codes,
        "characteristics": codes,
        "type": "ticket",
    }


def _assignments_to_timeline_items(
    technician: Technician,
    assignments: list[PlanningAssignment],
    *,
    planned_date: datetime | None,
    route_lookup: dict[tuple[int, int], tuple[int, float]] | None = None,
) -> list[dict[str, Any]]:
    if not assignments:
        return []

    assignments_by_day: dict[Any, list[PlanningAssignment]] = {}
    for assignment in assignments:
        day_key = assignment.planned_start_at.date() if assignment.planned_start_at else None
        assignments_by_day.setdefault(day_key, []).append(assignment)

    items: list[dict[str, Any]] = []
    for day_key in sorted(assignments_by_day, key=lambda value: value or planned_date.date() if planned_date else value):
        day_assignments = assignments_by_day[day_key]
        day_planned_date = (
            datetime.combine(day_key, time.min, tzinfo=planned_date.tzinfo if planned_date else None)
            if day_key is not None
            else planned_date
        )
        items.extend(
            _assignments_to_timeline_items_for_single_day(
                technician,
                day_assignments,
                planned_date=day_planned_date,
                route_lookup=route_lookup,
            )
        )
    return sorted(items, key=lambda item: item.get("planned_start_at") or "")


def _assignments_to_timeline_items_for_single_day(
    technician: Technician,
    assignments: list[PlanningAssignment],
    *,
    planned_date: datetime | None,
    route_lookup: dict[tuple[int, int], tuple[int, float]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    break_inserted = False
    pickup_inserted = False
    route_supply_requirement_codes = sorted({code for assignment in assignments for code in _supply_requirement_codes_from_ticket(assignment.ticket)})
    previous_end = _technician_day_start(technician, planned_date)
    previous_location_id = (
        technician.start_location.id if technician.start_location is not None else None
    )

    for assignment in assignments:
        travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
        assignment_supply_requirement_codes = _supply_requirement_codes_from_ticket(assignment.ticket)
        split_required_pickup = (
            bool(assignment_supply_requirement_codes)
            and not pickup_inserted
            and planned_date is not None
            and previous_location_id is not None
            and technician.branch is not None
            and technician.branch.location_id is not None
            and assignment.ticket.location_id is not None
        )

        if split_required_pickup:
            hq_location_id = technician.branch.location_id
            to_hq = (route_lookup or {}).get((previous_location_id, hq_location_id))
            hq_to_ticket = (route_lookup or {}).get((hq_location_id, assignment.ticket.location_id))
            pickup_duration = 5
            if to_hq is not None and hq_to_ticket is not None:
                to_hq_minutes, to_hq_distance = to_hq
                hq_to_ticket_minutes, hq_to_ticket_distance = hq_to_ticket
                travel_start = assignment.planned_start_at - timedelta(
                    minutes=to_hq_minutes + pickup_duration + hq_to_ticket_minutes
                )

                if not break_inserted:
                    break_item = _break_item_between(
                        technician.id,
                        previous_end,
                        travel_start,
                        planned_date=planned_date,
                    )
                    if break_item is not None:
                        items.append(break_item)
                        break_inserted = True

                if to_hq_minutes > 0:
                    items.append(
                        _travel_item(
                            f"travel-{assignment.id}-to-hq",
                            travel_start,
                            travel_start + timedelta(minutes=to_hq_minutes),
                            to_hq_minutes,
                            to_hq_distance,
                            before_ticket_id=assignment.ticket_id,
                            from_location_id=previous_location_id,
                            to_location_id=hq_location_id,
                        )
                    )
                pickup_start = travel_start + timedelta(minutes=to_hq_minutes)
                items.append(
                    _requirement_pickup_item(
                        technician.id,
                        hq_location_id,
                        route_supply_requirement_codes,
                        pickup_start,
                        duration_minutes=pickup_duration,
                    )
                )
                pickup_inserted = True
                hq_departure = pickup_start + timedelta(minutes=pickup_duration)
                if hq_to_ticket_minutes > 0:
                    items.append(
                        _travel_item(
                            f"travel-{assignment.id}-hq-to-ticket",
                            hq_departure,
                            assignment.planned_start_at,
                            hq_to_ticket_minutes,
                            hq_to_ticket_distance,
                            before_ticket_id=assignment.ticket_id,
                            from_location_id=hq_location_id,
                            to_location_id=assignment.ticket.location_id,
                        )
                    )
                items.append(_assignment_to_planning_item(assignment))
                previous_end = assignment.planned_end_at
                previous_location_id = assignment.ticket.location_id
                continue

        travel_start = assignment.planned_start_at - timedelta(minutes=travel_minutes)

        if not break_inserted:
            break_item = _break_item_between(
                technician.id,
                previous_end,
                travel_start,
                planned_date=planned_date,
            )
            if break_item is not None:
                items.append(break_item)
                break_inserted = True

        if assignment_supply_requirement_codes and not pickup_inserted and planned_date is not None:
            items.append(
                _requirement_pickup_item(
                    technician.id,
                    technician.branch.location_id if technician.branch else None,
                    route_supply_requirement_codes,
                    travel_start,
                )
            )
            pickup_inserted = True

        if travel_minutes > 0:
            items.append(_travel_item_before_assignment(assignment, travel_start))

        items.append(_assignment_to_planning_item(assignment))
        previous_end = assignment.planned_end_at
        previous_location_id = assignment.ticket.location_id

    if not break_inserted:
        route_end = _technician_day_end(technician, planned_date)
        break_item = _break_item_between(
            technician.id,
            previous_end,
            route_end,
            planned_date=planned_date,
        )
        if break_item is None and planned_date is not None:
            # Keep the break visible for empty routes or legacy plans where ticket
            # assignments do not leave a clean lunch-window gap.
            start_at = _datetime_at_minutes(planned_date, 11 * 60)
            break_item = _break_item(technician.id, start_at, 45)
        if break_item is not None:
            items.append(break_item)

    return sorted(items, key=lambda item: item.get("planned_start_at") or "")


def _requirement_pickup_item(
    technician_id: int,
    location_id: int | None,
    requirement_codes: list[str],
    at_time: datetime,
    duration_minutes: int = 5,
) -> dict[str, Any]:
    end_at = at_time + timedelta(minutes=duration_minutes)
    return {
        "id": f"requirement-pickup-{technician_id}-{at_time.strftime('%H%M')}",
        "title": "Hulpmiddelen ophalen",
        "type": "requirement_pickup",
        "display_variant": "requirement_pickup",
        "address": "HQ",
        "start": at_time.strftime("%H:%M"),
        "end": end_at.strftime("%H:%M"),
        "planned_start_at": at_time.isoformat(),
        "planned_end_at": end_at.isoformat(),
        "duration_minutes": duration_minutes,
        "location_id": location_id,
        "requirements": requirement_codes,
    }


def _travel_item_before_assignment(
    assignment: PlanningAssignment,
    travel_start: datetime,
) -> dict[str, Any]:
    travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
    return _travel_item(
        f"travel-{assignment.id}",
        travel_start,
        assignment.planned_start_at,
        travel_minutes,
        float(assignment.estimated_distance_km_before or 0),
        before_ticket_id=assignment.ticket_id,
    )


def _travel_item(
    item_id: str,
    start_at: datetime,
    end_at: datetime,
    travel_minutes: int,
    distance_km: float,
    *,
    before_ticket_id: int | None = None,
    from_location_id: int | None = None,
    to_location_id: int | None = None,
) -> dict[str, Any]:
    item = {
        "id": item_id,
        "title": "Rijtijd",
        "type": "travel",
        "start": start_at.strftime("%H:%M"),
        "end": end_at.strftime("%H:%M"),
        "planned_start_at": start_at.isoformat(),
        "planned_end_at": end_at.isoformat(),
        "duration_minutes": travel_minutes,
        "travel_minutes": travel_minutes,
        "distance_km": round(float(distance_km or 0), 1),
        "before_ticket_id": before_ticket_id,
    }
    if from_location_id is not None:
        item["from_location_id"] = from_location_id
    if to_location_id is not None:
        item["to_location_id"] = to_location_id
    return item


def _break_item_between(
    technician_id: int,
    start: datetime | None,
    end: datetime | None,
    *,
    planned_date: datetime | None,
) -> dict[str, Any] | None:
    if start is None or end is None or planned_date is None:
        return None
    lunch_start = _datetime_at_minutes(planned_date, 11 * 60)
    lunch_end = _datetime_at_minutes(planned_date, 13 * 60)
    duration_minutes = 45
    latest_start = lunch_end - timedelta(minutes=duration_minutes)
    break_start = max(start, lunch_start)
    if break_start > latest_start or break_start + timedelta(minutes=duration_minutes) > end:
        return None
    return _break_item(technician_id, break_start, duration_minutes)


def _break_item(technician_id: int, start_at: datetime, duration_minutes: int) -> dict[str, Any]:
    end_at = start_at + timedelta(minutes=duration_minutes)
    return {
        "id": f"break-{technician_id}-{start_at.strftime('%H%M')}",
        "title": "Lunch break",
        "type": "break",
        "start": start_at.strftime("%H:%M"),
        "end": end_at.strftime("%H:%M"),
        "planned_start_at": start_at.isoformat(),
        "planned_end_at": end_at.isoformat(),
        "duration_minutes": duration_minutes,
    }


def _technician_day_start(technician: Technician, planned_date: datetime | None) -> datetime | None:
    if planned_date is None:
        return None
    return _datetime_at_minutes(planned_date, int(getattr(technician, "workday_start_minutes", 8 * 60) or 8 * 60))


def _technician_day_end(technician: Technician, planned_date: datetime | None) -> datetime | None:
    if planned_date is None:
        return None
    return _datetime_at_minutes(planned_date, int(getattr(technician, "workday_end_minutes", 17 * 60) or 17 * 60))


def _datetime_at_minutes(anchor: datetime, minutes_after_midnight: int) -> datetime:
    return datetime.combine(anchor.date(), time.min, tzinfo=anchor.tzinfo).replace(
        hour=minutes_after_midnight // 60,
        minute=minutes_after_midnight % 60,
    )


def _count_open_tickets(session: Session, branch_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.branch_id == branch_id,
                Ticket.status.not_in(list(TERMINAL_TICKET_STATUSES)),
            )
        )
        or 0
    )


def _count_urgent_open_tickets(session: Session, branch_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.branch_id == branch_id,
                Ticket.urgency == TicketUrgency.URGENT,
                Ticket.status == TicketStatus.OPEN,
            )
        )
        or 0
    )


def _total_workday_minutes(technicians: list[Technician]) -> int:
    total = 0
    for technician in technicians:
        start = getattr(technician, "workday_start_minutes", None)
        end = getattr(technician, "workday_end_minutes", None)
        if start is not None and end is not None:
            total += max(0, int(end) - int(start))
        else:
            total += 8 * 60
    return total


def _move_existing_active_assignments(session: Session, branch_id: int) -> None:
    assignments = session.scalars(
        select(PlanningAssignment).where(
            PlanningAssignment.branch_id == branch_id,
            PlanningAssignment.status.in_(list(VISIBLE_ASSIGNMENT_STATUSES)),
        )
    ).all()
    ticket_ids = []
    for assignment in assignments:
        assignment.status = PlanningAssignmentStatus.MOVED
        ticket_ids.append(assignment.ticket_id)
    if ticket_ids:
        for ticket in session.scalars(select(Ticket).where(Ticket.id.in_(ticket_ids))).all():
            if ticket.status == TicketStatus.PLANNED:
                ticket.status = TicketStatus.OPEN
    session.flush()


def create_initial_planning_proposal(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    config = _config_from_payload(payload)
    technicians = load_available_technicians(session, config)
    tickets = load_candidate_tickets(session, config, technicians)
    matrix = get_planning_route_matrix(
        session,
        technicians,
        tickets,
        refresh_cache=config.refresh_route_cache,
    )
    day_plans = _build_horizon_plan(config, technicians, tickets, matrix)
    return _horizon_solution_as_dict(config, day_plans)


def run_initial_planning(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    config = _config_from_payload(payload)
    technicians = load_available_technicians(session, config)
    tickets = load_candidate_tickets(session, config, technicians)
    matrix = get_planning_route_matrix(
        session,
        technicians,
        tickets,
        refresh_cache=config.refresh_route_cache,
    )
    day_plans = _build_horizon_plan(config, technicians, tickets, matrix)

    planning_run = PlanningRun(
        branch_id=config.branch_id,
        trigger_type=PlanningRunTrigger.DAILY_START,
        status=PlanningRunStatus.RUNNING,
        planned_date=config.planned_date,
        started_at=datetime.utcnow(),
        notes=(
            "Initial planning generated for a multi-day horizon by multi-start "
            "randomized cheapest insertion + local search."
        ),
    )
    session.add(planning_run)
    session.flush()

    planned_ticket_ids: list[int] = []
    sequence_by_technician: dict[int, int] = {technician.id: 1 for technician in technicians}
    for day_plan in day_plans:
        optimizer: InitialRouteOptimizer = day_plan["optimizer"]
        solution: PlanningSolution = day_plan["solution"]
        for technician_id, route in solution.routes.items():
            stops = optimizer.build_stops(solution, technician_id)
            for stop in stops:
                assignment = PlanningAssignment(
                    planning_run_id=planning_run.id,
                    branch_id=config.branch_id,
                    technician_id=technician_id,
                    ticket_id=stop.ticket.id,
                    sequence_order=sequence_by_technician[technician_id],
                    planned_start_at=stop.planned_start_at,
                    planned_end_at=stop.planned_end_at,
                    estimated_duration_minutes=stop.ticket.service_minutes,
                    estimated_travel_minutes_before=stop.travel_minutes_before,
                    estimated_distance_km_before=stop.distance_km_before,
                    requires_hq_pickup=stop.requires_hq_pickup,
                    hq_location_id=stop.hq_location_id,
                    estimated_travel_minutes_to_hq=stop.travel_minutes_to_hq,
                    estimated_distance_km_to_hq=stop.distance_km_to_hq,
                    estimated_travel_minutes_hq_to_ticket=stop.travel_minutes_hq_to_ticket,
                    estimated_distance_km_hq_to_ticket=stop.distance_km_hq_to_ticket,
                    status=PlanningAssignmentStatus.PLANNED,
                    source=PlanningAssignmentSource.AI,
                )
                session.add(assignment)
                planned_ticket_ids.append(stop.ticket.id)
                sequence_by_technician[technician_id] += 1

    if planned_ticket_ids:
        for ticket in session.query(Ticket).filter(Ticket.id.in_(planned_ticket_ids)).all():
            ticket.status = TicketStatus.PLANNED

    summary = _horizon_summary(day_plans, tickets)
    planning_run.status = PlanningRunStatus.COMPLETED
    planning_run.completed_at = datetime.utcnow()
    planning_run.score_total_distance_km = round(summary["total_distance_km"], 3)
    planning_run.score_total_travel_minutes = int(summary["total_travel_minutes"])
    planning_run.score_completed_tickets = int(summary["completed_tickets"])
    planning_run.score_unplanned_tickets = int(summary["unplanned_tickets"])

    result = _horizon_solution_as_dict(config, day_plans)
    result["planning_run_id"] = planning_run.id
    return result


def _config_from_payload(payload: dict[str, Any]) -> PlanningConfig:
    planned_date = payload.get("planned_date") or datetime.utcnow()
    if isinstance(planned_date, str):
        planned_date = datetime.fromisoformat(planned_date.replace("Z", "+00:00"))
    if not isinstance(planned_date, datetime):
        raise PlanningAiError("planned_date must be a datetime or ISO datetime string")

    branch_id = int(payload.get("branch_id") or 1)
    return PlanningConfig(
        branch_id=branch_id,
        planned_date=planned_date,
        max_candidates_per_technician=int(payload.get("max_candidates_per_technician") or 0),
        initial_non_urgent_minutes_per_technician=int(
            payload.get("initial_non_urgent_minutes_per_technician") or 360
        ),
        initial_route_work_minutes_per_technician=int(
            payload.get("initial_route_work_minutes_per_technician")
            or payload.get("initial_planned_minutes_per_technician")
            or 360
        ),
        latest_ticket_start_route_work_minutes=int(
            payload.get("latest_ticket_start_route_work_minutes") or 300
        ),
        travel_penalty_per_minute=int(payload.get("travel_penalty_per_minute") or 25),
        planning_horizon_days=max(1, int(payload.get("planning_horizon_days") or 3)),
        defer_to_day_2_penalty_minutes=int(
            payload.get("defer_to_day_2_penalty_minutes") or 45
        ),
        defer_to_day_3_penalty_minutes=int(
            payload.get("defer_to_day_3_penalty_minutes") or 120
        ),
        default_service_minutes=int(payload.get("default_service_minutes") or 60),
        multi_start_iterations=int(payload.get("multi_start_iterations") or 40),
        local_search_iterations=int(payload.get("local_search_iterations") or 250),
        random_seed=payload.get("random_seed", 42),
        refresh_route_cache=bool(payload.get("refresh_route_cache", False)),
        low_priority_max_extra_travel_minutes=int(
            payload.get("low_priority_max_extra_travel_minutes") or 35
        ),
        break_duration_minutes=int(payload.get("break_duration_minutes") or 45),
        break_window_start_minutes=int(payload.get("break_window_start_minutes") or 11 * 60),
        break_window_end_minutes=int(payload.get("break_window_end_minutes") or 13 * 60),
        requirement_pickup_duration_minutes=int(
            payload.get("requirement_pickup_duration_minutes") or 5
        ),
    )


def _build_horizon_plan(
    config: PlanningConfig,
    technicians: list[Any],
    tickets: list[Any],
    matrix: Any,
) -> list[dict[str, Any]]:
    """Plan all candidate tickets across the configured number of days.

    The expensive initial run is meant to be started overnight. Therefore it
    considers every open ticket once, then repeatedly plans the remaining work
    for the next day in the horizon. The existing optimizer still enforces the
    per-day non-urgent cap, so each mechanic keeps room for same-day urgent
    tickets on every planned day.
    """
    remaining_by_id = {ticket.id: ticket for ticket in tickets}
    day_plans: list[dict[str, Any]] = []

    for day_index in range(max(1, config.planning_horizon_days)):
        if not remaining_by_id and day_index > 0:
            break
        if day_index == 0:
            defer_unplanned_penalty_minutes = config.defer_to_day_2_penalty_minutes
        elif day_index == 1:
            defer_unplanned_penalty_minutes = config.defer_to_day_3_penalty_minutes
        else:
            defer_unplanned_penalty_minutes = 0

        day_config = replace(
            config,
            planned_date=config.planned_date + timedelta(days=day_index),
            random_seed=(config.random_seed + day_index if isinstance(config.random_seed, int) else config.random_seed),
            defer_unplanned_penalty_minutes=defer_unplanned_penalty_minutes,
        )
        remaining_tickets = sorted(
            remaining_by_id.values(),
            key=lambda ticket: (
                ticket.urgency_rank,
                -len(ticket.requirement_codes),
                ticket.created_at,
                ticket.id,
            ),
        )
        optimizer = InitialRouteOptimizer(
            config=day_config,
            technicians=technicians,
            tickets=remaining_tickets,
            matrix=matrix,
        )
        solution = optimizer.optimize()
        planned_ids = {
            stop.ticket.id
            for technician_id in solution.routes
            for stop in optimizer.build_stops(solution, technician_id)
        }
        for ticket_id in planned_ids:
            remaining_by_id.pop(ticket_id, None)
        day_plans.append(
            {
                "day_index": day_index,
                "config": day_config,
                "optimizer": optimizer,
                "solution": solution,
                "planned_ticket_ids": planned_ids,
            }
        )


    return day_plans



def _horizon_summary(day_plans: list[dict[str, Any]], all_tickets: list[Any]) -> dict[str, Any]:
    planned_ids: set[int] = set()
    total_travel_minutes = 0
    total_distance_km = 0.0
    sla_misses = 0
    overtime_minutes = 0

    for day_plan in day_plans:
        solution: PlanningSolution = day_plan["solution"]
        planned_ids.update(day_plan.get("planned_ticket_ids", set()))
        total_travel_minutes += solution.total_travel_minutes
        total_distance_km += solution.total_distance_km
        sla_misses += solution.sla_misses
        overtime_minutes += solution.overtime_minutes

    return {
        "completed_tickets": len(planned_ids),
        "unplanned_tickets": len({ticket.id for ticket in all_tickets} - planned_ids),
        "sla_misses": sla_misses,
        "overtime_minutes": overtime_minutes,
        "total_travel_minutes": total_travel_minutes,
        "total_distance_km": total_distance_km,
    }


def _horizon_solution_as_dict(config: PlanningConfig, day_plans: list[dict[str, Any]]) -> dict[str, Any]:
    day_results = [
        _solution_as_dict(day_plan["config"], day_plan["optimizer"], day_plan["solution"])
        for day_plan in day_plans
    ]
    all_ticket_ids = {
        ticket.id
        for day_plan in day_plans
        for ticket in day_plan["optimizer"].tickets
    }
    planned_ids = {ticket_id for day_plan in day_plans for ticket_id in day_plan.get("planned_ticket_ids", set())}
    final_optimizer = day_plans[-1]["optimizer"] if day_plans else None
    final_unplanned_ids = sorted(all_ticket_ids - planned_ids)

    unplanned = []
    if final_optimizer is not None:
        for ticket_id in final_unplanned_ids:
            ticket = final_optimizer.ticket_by_id.get(ticket_id)
            if ticket is None:
                continue
            unplanned.append(
                {
                    "ticket_id": ticket.id,
                    "urgency": ticket.urgency.value,
                    "deadline_at": ticket.deadline_at.isoformat(),
                    "subject": ticket.subject,
                    "address": ticket.address,
                    "reason": "No feasible route position found within the 3-day workday/capacity horizon",
                }
            )

    summary = {
        "completed_tickets": len(planned_ids),
        "unplanned_tickets": len(final_unplanned_ids),
        "sla_misses": sum(day["summary"]["sla_misses"] for day in day_results),
        "overtime_minutes": sum(day["summary"]["overtime_minutes"] for day in day_results),
        "total_travel_minutes": sum(day["summary"]["total_travel_minutes"] for day in day_results),
        "total_distance_km": round(sum(day["summary"]["total_distance_km"] for day in day_results), 3),
        "planning_horizon_days": config.planning_horizon_days,
        "planned_service_minutes_per_technician_per_day": config.initial_non_urgent_minutes_per_technician,
        "planned_route_work_minutes_per_technician_per_day": config.initial_route_work_minutes_per_technician,
        "latest_ticket_start_route_work_minutes": config.latest_ticket_start_route_work_minutes,
        "reserved_urgent_minutes_per_technician_per_day": max(
            0, 8 * 60 - config.initial_route_work_minutes_per_technician
        ),
        "travel_penalty_per_minute": config.travel_penalty_per_minute,
        "defer_to_day_2_penalty_minutes": config.defer_to_day_2_penalty_minutes,
        "defer_to_day_3_penalty_minutes": config.defer_to_day_3_penalty_minutes,
        "multi_start_iterations": config.multi_start_iterations,
        "local_search_iterations": config.local_search_iterations,
        "random_seed": config.random_seed,
    }

    routes = []
    for day in day_results:
        day_date = day["planned_date"]
        for route in day["routes"]:
            route = dict(route)
            route["planning_day"] = day_date
            routes.append(route)

    return {
        "algorithm": "multi_day_randomized_cheapest_insertion_plus_local_search",
        "branch_id": config.branch_id,
        "planned_date": config.planned_date.isoformat(),
        "planning_horizon_days": config.planning_horizon_days,
        "score": round(sum(day["score"] for day in day_results), 3),
        "summary": summary,
        "design_choices": [
            "The overnight initial plan considers all open candidate tickets, not only a small earliest-deadline slice.",
            "The plan is built for the next 3 days by default.",
            "Each mechanic receives about 5-6 hours of planned route workload per day, counting service, travel and HQ pickup time, leaving same-day capacity for incoming urgent jobs.",
            "Medium and low tickets share the same planning class; medium only has a tiny score tie-breaker that travel time can outweigh.",
            "Travel, lunch breaks and HQ requirement pickups remain explicit timeline items.",
            "Travel to HQ is stored on the assignment that needs the pickup and rendered as its own route leg on the map.",
        ],
        "routes": routes,
        "days": day_results,
        "unplanned_tickets": unplanned,
        "notes": [
            "Multi-day horizon planning",
            "All open feasible tickets are considered",
            "Daily non-urgent capacity is capped per mechanic to reserve urgent capacity",
        ],
    }


def _solution_as_dict(
    config: PlanningConfig,
    optimizer: InitialRouteOptimizer,
    solution: PlanningSolution,
) -> dict[str, Any]:
    routes = []
    for technician_id, route in solution.routes.items():
        timeline = optimizer.build_timeline(solution, technician_id, include_return_home=True)
        stops = [item for item in timeline if isinstance(item, PlannedStop)]
        routes.append(
            {
                "technician_id": technician_id,
                "technician_name": route.technician.name,
                "start_location_id": route.technician.start_location_id,
                "end_location_id": route.technician.end_location_id,
                "workday_start_minutes": route.technician.workday_start_minutes,
                "workday_end_minutes": route.technician.workday_end_minutes,
                "timeline": [item.as_dict() for item in timeline],
                "stops": [stop.as_dict() for stop in stops],
                "ticket_count": len(stops),
                "break_count": sum(1 for item in timeline if isinstance(item, PlannedBreak)),
                "total_break_minutes": _route_break_minutes(timeline),
                "total_travel_minutes": _route_travel_minutes(timeline),
                "total_distance_km": round(_route_distance_km(timeline), 3),
            }
        )

    unplanned = []
    for ticket_id in sorted(solution.unplanned_ticket_ids):
        ticket = optimizer.ticket_by_id[ticket_id]
        unplanned.append(
            {
                "ticket_id": ticket.id,
                "urgency": ticket.urgency.value,
                "deadline_at": ticket.deadline_at.isoformat(),
                "subject": ticket.subject,
                "address": ticket.address,
                "reason": "No feasible route position found within workday/capacity rules",
            }
        )

    return {
        "algorithm": "multi_start_randomized_cheapest_insertion_plus_local_search",
        "branch_id": config.branch_id,
        "planned_date": config.planned_date.isoformat(),
        "score": round(solution.score, 3),
        "summary": {
            "completed_tickets": solution.completed_tickets,
            "unplanned_tickets": len(solution.unplanned_ticket_ids),
            "sla_misses": solution.sla_misses,
            "overtime_minutes": solution.overtime_minutes,
            "total_travel_minutes": solution.total_travel_minutes,
            "total_distance_km": round(solution.total_distance_km, 3),
            "multi_start_iterations": config.multi_start_iterations,
            "local_search_iterations": config.local_search_iterations,
            "random_seed": config.random_seed,
            "planned_route_work_minutes_per_technician": (
                config.initial_route_work_minutes_per_technician
            ),
            "latest_ticket_start_route_work_minutes": (
                config.latest_ticket_start_route_work_minutes
            ),
            "travel_penalty_per_minute": config.travel_penalty_per_minute,
            "defer_unplanned_penalty_minutes": config.defer_unplanned_penalty_minutes,
            "penalty_weights": {
                "sla_miss": SLA_MISS_PENALTY,
                "unplanned_base_per_ticket": UNPLANNED_TICKET_PENALTY,
                "unplanned_urgent_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.URGENT],
                "unplanned_medium_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.MEDIUM],
                "unplanned_low_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.LOW],
                "overtime_per_minute": OVERTIME_PENALTY_PER_MINUTE,
                "travel_per_minute": config.travel_penalty_per_minute,
                "defer_unplanned_per_ticket": (
                    config.defer_unplanned_penalty_minutes * config.travel_penalty_per_minute
                ),
            },
        },
        "design_choices": [
            "Multiple randomized start plans are tried to avoid all nearby-home mechanics staying in the same area.",
            "Each start plan is improved with move, swap and reorder operations.",
            "All feasible tickets in the 3-day horizon are added; low priority is not treated as optional filler work.",
            "Every mechanic gets a 45 minute break planned inside the 11:00-13:00 window.",
            "A route with one or more supply requirements gets one HQ pickup before the first supply ticket.",
            "Travel and break blocks are returned as explicit timeline items, instead of appearing as gaps between tickets.",
            "Deadline misses are soft score penalties, not hard feasibility blockers; medium and low are not separated into priority bands.",
        ],
        "routes": routes,
        "unplanned_tickets": unplanned,
        "notes": solution.algorithm_notes,
    }


def _route_travel_minutes(items: list[Any]) -> int:
    return sum(item.travel_minutes for item in items if isinstance(item, PlannedTravel))


def _route_distance_km(items: list[Any]) -> float:
    return sum(item.distance_km for item in items if isinstance(item, PlannedTravel))


def _route_break_minutes(items: list[Any]) -> int:
    return sum(item.duration_minutes for item in items if isinstance(item, PlannedBreak))
