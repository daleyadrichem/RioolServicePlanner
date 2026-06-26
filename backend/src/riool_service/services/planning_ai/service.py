from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.db_utils import get_engine
from riool_service.database.models.base import Base
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
from riool_service.database.models.technician import Technician
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.services.planning_ai.models import (
    PlannedBreak,
    PlannedStop,
    PlannedTravel,
    PlanningConfig,
    PlanningSolution,
)
from riool_service.services.planning_ai.optimizer import InitialRouteOptimizer
from riool_service.services.planning_ai.routing import get_planning_route_matrix
from riool_service.services.planning_ai.selection import load_available_technicians, load_candidate_tickets


class PlanningAiError(ValueError):
    pass


def ensure_planning_ai_tables() -> None:
    Base.metadata.create_all(get_engine())




TERMINAL_TICKET_STATUSES = {TicketStatus.COMPLETED, TicketStatus.CANCELLED}
VISIBLE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.IN_PROGRESS,
}


def get_planning_overview(session: Session, *, branch_id: int | None = None) -> dict[str, Any]:
    """Return the current planner board built from persisted assignments.

    The frontend uses this to decide whether to show "Start planning" or
    "Herplannen" and to render the actual tickets assigned to each mechanic.
    "Urgent open" intentionally means urgent tickets with ticket status OPEN;
    planned-but-not-finished tickets are no longer counted as open here.
    """
    branch = _overview_branch(session, branch_id)
    latest_run = _latest_completed_planning_run(session, branch.id)
    technicians = _overview_technicians(session, branch.id)
    assignments = _overview_assignments(session, latest_run.id if latest_run else None)

    assignments_by_technician: dict[int, list[PlanningAssignment]] = {}
    for assignment in assignments:
        assignments_by_technician.setdefault(assignment.technician_id, []).append(assignment)

    columns = []
    for technician in technicians:
        technician_assignments = assignments_by_technician.get(technician.id, [])
        columns.append(
            {
                "technician": _technician_to_overview_dict(technician),
                "items": _assignments_to_timeline_items(
                    technician,
                    technician_assignments,
                    planned_date=latest_run.planned_date if latest_run else None,
                ),
            }
        )

    assigned_ticket_ids = {assignment.ticket_id for assignment in assignments}
    total_open = _count_open_tickets(session, branch.id)
    urgent_open = _count_urgent_open_tickets(session, branch.id)
    total_minutes = _total_workday_minutes(technicians)
    used_minutes = sum(
        int(assignment.estimated_duration_minutes or 0) + int(assignment.estimated_travel_minutes_before or 0)
        for assignment in assignments
    )
    if latest_run is not None:
        used_minutes += len(technicians) * 45
    travel_minutes = sum(int(assignment.estimated_travel_minutes_before or 0) for assignment in assignments)
    kilometers = round(sum(float(assignment.estimated_distance_km_before or 0) for assignment in assignments), 1)

    return {
        "has_plan": latest_run is not None,
        "planning_run_id": latest_run.id if latest_run else None,
        "planned_date": latest_run.planned_date.isoformat() if latest_run and latest_run.planned_date else None,
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
    return sorted(
        {
            link.requirement.code.upper()
            for link in ticket.ticket_requirements
            if link.requirement is not None and link.requirement.code is not None
        }
    )


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
        "urgency": _value(ticket.urgency),
        "status": _value(ticket.status),
        "assignment_status": _value(assignment.status),
        "description": ticket.description,
        "requires_ladder": "LADDER" in codes,
        "requires_spring": "VEER" in codes,
        "requirements": codes,
        "characteristics": codes,
        "type": "ticket",
    }


def _assignments_to_timeline_items(
    technician: Technician,
    assignments: list[PlanningAssignment],
    *,
    planned_date: datetime | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    break_inserted = False
    previous_end = _technician_day_start(technician, planned_date)

    for assignment in assignments:
        travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
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

        if travel_minutes > 0:
            items.append(_travel_item_before_assignment(assignment, travel_start))

        items.append(_assignment_to_planning_item(assignment))
        previous_end = assignment.planned_end_at

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


def _travel_item_before_assignment(
    assignment: PlanningAssignment,
    travel_start: datetime,
) -> dict[str, Any]:
    travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
    travel_end = assignment.planned_start_at
    return {
        "id": f"travel-{assignment.id}",
        "title": "Rijtijd",
        "type": "travel",
        "start": travel_start.strftime("%H:%M"),
        "end": travel_end.strftime("%H:%M"),
        "planned_start_at": travel_start.isoformat(),
        "planned_end_at": travel_end.isoformat(),
        "duration_minutes": travel_minutes,
        "travel_minutes": travel_minutes,
        "distance_km": round(float(assignment.estimated_distance_km_before or 0), 1),
        "before_ticket_id": assignment.ticket_id,
    }


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
    optimizer = InitialRouteOptimizer(
        config=config,
        technicians=technicians,
        tickets=tickets,
        matrix=matrix,
    )
    solution = optimizer.optimize()
    return _solution_as_dict(config, optimizer, solution)


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
    optimizer = InitialRouteOptimizer(
        config=config,
        technicians=technicians,
        tickets=tickets,
        matrix=matrix,
    )
    solution = optimizer.optimize()

    planning_run = PlanningRun(
        branch_id=config.branch_id,
        trigger_type=PlanningRunTrigger.DAILY_START,
        status=PlanningRunStatus.RUNNING,
        planned_date=config.planned_date,
        started_at=datetime.utcnow(),
        notes="Initial planning generated by multi-start randomized cheapest insertion + local search.",
    )
    session.add(planning_run)
    session.flush()

    planned_ticket_ids: list[int] = []
    for technician_id, route in solution.routes.items():
        stops = optimizer.build_stops(solution, technician_id)
        for sequence_order, stop in enumerate(stops, start=1):
            assignment = PlanningAssignment(
                planning_run_id=planning_run.id,
                branch_id=config.branch_id,
                technician_id=technician_id,
                ticket_id=stop.ticket.id,
                sequence_order=sequence_order,
                planned_start_at=stop.planned_start_at,
                planned_end_at=stop.planned_end_at,
                estimated_duration_minutes=stop.ticket.service_minutes,
                estimated_travel_minutes_before=stop.travel_minutes_before,
                estimated_distance_km_before=stop.distance_km_before,
                status=PlanningAssignmentStatus.PLANNED,
                source=PlanningAssignmentSource.AI,
            )
            session.add(assignment)
            planned_ticket_ids.append(stop.ticket.id)

    if planned_ticket_ids:
        for ticket in session.query(Ticket).filter(Ticket.id.in_(planned_ticket_ids)).all():
            ticket.status = TicketStatus.PLANNED

    planning_run.status = PlanningRunStatus.COMPLETED
    planning_run.completed_at = datetime.utcnow()
    planning_run.score_total_distance_km = round(solution.total_distance_km, 3)
    planning_run.score_total_travel_minutes = solution.total_travel_minutes
    planning_run.score_completed_tickets = solution.completed_tickets
    planning_run.score_unplanned_tickets = len(solution.unplanned_ticket_ids)

    result = _solution_as_dict(config, optimizer, solution)
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
        max_candidates_per_technician=int(payload.get("max_candidates_per_technician") or 10),
        initial_non_urgent_minutes_per_technician=int(payload.get("initial_non_urgent_minutes_per_technician") or 360),
        default_service_minutes=int(payload.get("default_service_minutes") or 60),
        multi_start_iterations=int(payload.get("multi_start_iterations") or 40),
        local_search_iterations=int(payload.get("local_search_iterations") or 250),
        random_seed=payload.get("random_seed", 42),
        refresh_route_cache=bool(payload.get("refresh_route_cache", False)),
        low_priority_max_extra_travel_minutes=int(payload.get("low_priority_max_extra_travel_minutes") or 35),
        break_duration_minutes=int(payload.get("break_duration_minutes") or 45),
        break_window_start_minutes=int(payload.get("break_window_start_minutes") or 11 * 60),
        break_window_end_minutes=int(payload.get("break_window_end_minutes") or 13 * 60),
    )


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
                "reason": "No feasible route position found within SLA/workday/capacity rules",
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
        },
        "design_choices": [
            "Multiple randomized start plans are tried to avoid all nearby-home mechanics staying in the same area.",
            "Each start plan is improved with move, swap and reorder operations.",
            "Low-priority tickets are added only when they fit well and do not cause SLA/workday issues.",
            "Every mechanic gets a 45 minute break planned inside the 11:00-13:00 window.",
            "Travel and break blocks are returned as explicit timeline items, instead of appearing as gaps between tickets.",
            "Urgent and earliest-deadline tickets are protected first.",
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
