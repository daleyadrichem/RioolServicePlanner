from __future__ import annotations

from dataclasses import replace
import logging
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
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
from riool_service.database.models.simulation_state import SimulationState
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.route_cache import RouteCache, RouteProvider
from riool_service.database.models.technician import Technician
from riool_service.database.models.technician_availability import TechnicianAvailability
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.services.planning_ai.models import (
    PlannedBreak,
    PlannedStop,
    PlannedTravel,
    PlannedRequirementPickup,
    PlanningConfig,
    PlanningSolution,
    TicketInput,
)
from riool_service.services.planning_ai.optimizer import (
    InitialRouteOptimizer,
    SLA_MISS_PENALTY,
    UNPLANNED_TICKET_PENALTY,
    UNPLANNED_URGENCY_TIEBREAKER,
    OVERTIME_PENALTY_PER_MINUTE,
)
from riool_service.services.planning_ai.routing import get_cached_planning_route_matrix, get_incremental_planning_route_matrix, get_planning_route_matrix
from riool_service.services.planning_ai.selection import load_available_technicians, load_candidate_tickets


logger = logging.getLogger(__name__)

SUPPLY_REQUIREMENT_CODES = {"SUPPLIES"}
PLANNING_DEBUG_LOG_DIR = Path("logs/planning_runs")


class PlanningAiError(ValueError):
    pass


class ActivePlanningRunError(PlanningAiError):
    pass


def ensure_planning_ai_tables() -> None:
    create_schema(get_engine())


def _planning_debug_log_path(planning_run_id: int) -> Path:
    return PLANNING_DEBUG_LOG_DIR / f"planning_run_{planning_run_id}.jsonl"


def _append_planning_debug_log(path: Path, message: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "message": message,
        **fields,
    }
    line = json.dumps(payload, default=str, sort_keys=True)
    # Also print the JSONL debug line to the backend console. This makes
    # operational replanning problems visible immediately when running uvicorn.
    print(f"[planning-ai-debug] {line}", flush=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")



def _debug_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _debug_assignment_dict(assignment: PlanningAssignment) -> dict[str, Any]:
    ticket = assignment.ticket
    return {
        "assignment_id": assignment.id,
        "ticket_id": assignment.ticket_id,
        "ticket_number": getattr(ticket, "ticket_number", None) or getattr(ticket, "number", None),
        "technician_id": assignment.technician_id,
        "assignment_status": _value(assignment.status),
        "ticket_status": _value(ticket.status) if ticket is not None else None,
        "start": _debug_datetime(assignment.planned_start_at),
        "end": _debug_datetime(assignment.planned_end_at),
        "sequence_order": assignment.sequence_order,
        "ticket_location_id": getattr(ticket, "location_id", None),
        "locked_by_planner": assignment.locked_by_planner,
    }


def _debug_ticket_input_dict(ticket: TicketInput) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "location_id": ticket.location_id,
        "service_minutes": ticket.service_minutes,
        "urgency": _value(ticket.urgency),
        "urgency_rank": ticket.urgency_rank,
        "created_at": _debug_datetime(ticket.created_at),
        "requirement_codes": sorted(ticket.requirement_codes),
        "deadline_at": _debug_datetime(ticket.deadline_at),
    }


def _debug_technician_input_dict(technician: Any) -> dict[str, Any]:
    return {
        "technician_id": technician.id,
        "name": getattr(technician, "name", None),
        "start_location_id": technician.start_location_id,
        "end_location_id": technician.end_location_id,
        "office_location_id": technician.office_location_id,
        "workday_start_minutes": technician.workday_start_minutes,
        "workday_end_minutes": technician.workday_end_minutes,
        "requirement_codes": sorted(getattr(technician, "requirement_codes", [])),
    }


TERMINAL_TICKET_STATUSES = {TicketStatus.COMPLETED, TicketStatus.CANCELLED}
VISIBLE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.DRIVING,
    PlanningAssignmentStatus.COMPLETED,
}
# Assignments that must remain part of the incremental replanning base.
# COMPLETED assignments are intentionally included here so a same-day urgent
# replanning run cannot reuse a time slot that has already happened.
REPLANNING_BASE_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.DRIVING,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.COMPLETED,
}
# Operationally fixed work.  The incremental replanner may preserve/copy these
# assignments into a new run, but it must not insert work before them, move them
# to another technician/day, defer them, or overwrite their ticket status.
LOCKED_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.DRIVING,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.COMPLETED,
}
LOCKED_TICKET_STATUSES = {
    TicketStatus.IN_PROGRESS,
    TicketStatus.DELAYED,
    TicketStatus.COMPLETED,
}
ACTIVE_PLANNING_RUN_STATUSES = {PlanningRunStatus.PENDING, PlanningRunStatus.RUNNING}
PLANNING_WORKER_ACTIVE_TICKET_STATUSES = {
    TicketStatus.OPEN,
    TicketStatus.PLANNED,
    TicketStatus.IN_PROGRESS,
    TicketStatus.DELAYED,
}
PLANNING_WORKER_MIN_INITIAL_PLAN_COVERAGE = 0.20


def _planning_run_to_status_dict(planning_run: PlanningRun | None) -> dict[str, Any] | None:
    if planning_run is None:
        return None
    return {
        "id": planning_run.id,
        "branch_id": planning_run.branch_id,
        "status": _value(planning_run.status),
        "trigger_type": _value(planning_run.trigger_type),
        "planned_date": planning_run.planned_date.isoformat() if planning_run.planned_date else None,
        "started_at": planning_run.started_at.isoformat() if planning_run.started_at else None,
        "completed_at": planning_run.completed_at.isoformat() if planning_run.completed_at else None,
        "error_message": planning_run.error_message,
    }


def _active_planning_run(session: Session, branch_id: int) -> PlanningRun | None:
    return session.scalar(
        select(PlanningRun)
        .where(PlanningRun.branch_id == branch_id, PlanningRun.status.in_(list(ACTIVE_PLANNING_RUN_STATUSES)))
        .order_by(PlanningRun.started_at.desc().nullslast(), PlanningRun.id.desc())
        .limit(1)
    )


def _create_visible_planning_run(
    session: Session,
    *,
    branch_id: int,
    trigger_type: PlanningRunTrigger,
    planned_date: datetime,
    notes: str,
) -> PlanningRun:
    active_run = _active_planning_run(session, branch_id)
    if active_run is not None:
        raise ActivePlanningRunError(
            f"Planning run {active_run.id} is already {_value(active_run.status).lower()} for branch {branch_id}."
        )
    planning_run = PlanningRun(
        branch_id=branch_id,
        trigger_type=trigger_type,
        status=PlanningRunStatus.RUNNING,
        planned_date=planned_date,
        started_at=datetime.utcnow(),
        notes=notes,
    )
    session.add(planning_run)
    session.commit()
    session.refresh(planning_run)
    return planning_run


def _mark_planning_run_failed(session: Session, planning_run: PlanningRun | None, exc: BaseException) -> None:
    if planning_run is None:
        return
    planning_run_id = planning_run.id
    try:
        session.rollback()
        persisted_run = session.get(PlanningRun, planning_run_id)
        if persisted_run is None:
            return
        persisted_run.status = PlanningRunStatus.FAILED
        persisted_run.completed_at = datetime.utcnow()
        persisted_run.error_message = str(exc)[:2000]
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to mark planning_run_id=%s as FAILED", planning_run_id)


def get_planning_overview(session: Session, *, branch_id: int | None = None, planned_date: str | date | datetime | None = None) -> dict[str, Any]:
    """Return the current planner board built from persisted assignments.

    The frontend uses this to decide whether to show "Start planning" or
    "Herplannen" and to render the actual tickets assigned to each mechanic.
    "Urgent open" intentionally means urgent tickets with ticket status OPEN;
    planned-but-not-finished tickets are no longer counted as open here.
    """
    branch = _overview_branch(session, branch_id)
    active_run = _active_planning_run(session, branch.id)
    latest_run = _latest_completed_planning_run(session, branch.id)
    technicians = _overview_technicians(session, branch.id)
    all_assignments = _overview_assignments(session, latest_run.id if latest_run else None)
    available_dates = _available_assignment_dates(all_assignments)
    selected_day = _selected_planning_day(planned_date, available_dates, latest_run.planned_date if latest_run else None)
    availability_by_technician_id = _availability_map(session, branch.id, selected_day) if selected_day is not None else {}
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
                "technician": {
                    **_technician_to_overview_dict(technician),
                    "is_available": availability_by_technician_id.get(int(technician.id), True),
                },
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
    return_travel_minutes, return_distance_km = _return_travel_from_assignments(
        technicians, assignments_by_technician, route_lookup
    )
    used_minutes += return_travel_minutes
    travel_minutes = (
        sum(int(assignment.estimated_travel_minutes_before or 0) for assignment in assignments)
        + return_travel_minutes
    )
    kilometers = round(
        sum(float(assignment.estimated_distance_km_before or 0) for assignment in assignments)
        + return_distance_km,
        1,
    )

    return {
        "has_plan": latest_run is not None,
        "branch_id": branch.id,
        "planning_run_id": latest_run.id if latest_run else None,
        "planning_status": _value(active_run.status) if active_run else (_value(latest_run.status) if latest_run else None),
        "is_planning_running": active_run is not None,
        "active_planning_run": _planning_run_to_status_dict(active_run),
        "latest_completed_planning_run": _planning_run_to_status_dict(latest_run),
        "planned_date": selected_day.isoformat() if selected_day else (latest_run.planned_date.isoformat() if latest_run and latest_run.planned_date else None),
        "available_dates": [value.isoformat() for value in available_dates],
        "unavailable_technician_ids": sorted([
            technician_id for technician_id, is_available in availability_by_technician_id.items() if not is_available
        ]),
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


def plan_new_ticket_incrementally(
    session: Session,
    ticket_id: int,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Insert one newly-open ticket into the latest plan without full replanning.

    This is intentionally not the overnight multi-start optimizer. It starts
    from the latest persisted planning run, tries the new ticket in the best
    insertion position, and only uses a small local repair step around that
    insertion. If today is too full for an urgent ticket, non-urgent tickets from
    the active planning day may be pushed to later horizon days with a soft
    reschedule penalty.
    """
    payload = payload or {}
    logger.debug("Incremental replanner requested for ticket_id=%s payload=%s", ticket_id, payload)
    ticket = _load_ticket_for_planning(session, ticket_id)
    if ticket is None:
        logger.debug("Incremental replanner skipped ticket_id=%s: ticket not found", ticket_id)
        return None
    if ticket.status != TicketStatus.OPEN:
        logger.debug(
            "Incremental replanner skipped ticket_id=%s: status=%s is not OPEN",
            ticket_id,
            ticket.status,
        )
        return None

    logger.debug(
        "Incremental replanner loaded ticket_id=%s branch_id=%s urgency=%s deadline=%s created_at=%s",
        ticket.id,
        ticket.branch_id,
        ticket.urgency,
        ticket.deadline_at,
        ticket.created_at,
    )

    latest_run = _latest_completed_planning_run(session, ticket.branch_id)
    if latest_run is None:
        logger.debug(
            "Incremental replanner cannot plan ticket_id=%s: no completed planning run for branch_id=%s",
            ticket_id,
            ticket.branch_id,
        )
        return {
            "planned": False,
            "ticket_id": ticket_id,
            "reason": "No completed planning run exists yet; incremental insertion needs an existing plan.",
        }

    readiness = _planning_worker_readiness_for_branch(
        session,
        int(ticket.branch_id),
        latest_run.planned_date.date(),
    )
    if not readiness.get("ready"):
        logger.debug(
            "Incremental replanner paused for ticket_id=%s branch_id=%s: %s",
            ticket_id,
            ticket.branch_id,
            readiness,
        )
        return {
            "planned": False,
            "ticket_id": ticket_id,
            "reason": "Initial planning has not been completed for the active planning day yet.",
            "readiness": readiness,
        }

    config_payload = {
        "branch_id": ticket.branch_id,
        "planned_date": latest_run.planned_date,
        **payload,
    }
    config = _config_from_payload(config_payload)
    config = replace(
        config,
        multi_start_iterations=1,
        # Incremental insertion must be a seconds-level operation. Do not run
        # the normal stochastic/local-search improvement loop here; it can
        # examine thousands of move/swap/2-opt candidates and accidentally turn
        # one-ticket insertion into a near-full replanning pass.
        local_search_iterations=0,
    )
    logger.debug(
        "Incremental replanner using latest_run_id=%s planned_date=%s horizon_days=%s local_search_iterations=%s stop_condition=direct_insert_or_minimal_tail_deferral_no_local_search",
        latest_run.id,
        latest_run.planned_date,
        config.planning_horizon_days,
        config.local_search_iterations,
    )

    technicians = load_available_technicians(session, config)
    unavailable_technician_ids = _unavailable_technician_ids_for_date(
        session,
        config.branch_id,
        config.planned_date.date(),
    )
    available_today_technician_ids = [
        int(technician.id)
        for technician in technicians
        if int(technician.id) not in unavailable_technician_ids
    ]
    if not available_today_technician_ids:
        raise PlanningAiError(
            f"No available technicians found for branch {config.branch_id} on {config.planned_date.date().isoformat()}"
        )
    logger.debug(
        "Incremental replanner loaded %s active technician(s), %s available today: active=%s available_today=%s unavailable_today=%s",
        len(technicians),
        len(available_today_technician_ids),
        [technician.id for technician in technicians],
        available_today_technician_ids,
        sorted(unavailable_technician_ids),
    )
    active_assignments = _replanning_base_assignments(session, latest_run.id)
    logger.debug(
        "Incremental replanner loaded %s visible active assignment(s) from latest_run_id=%s",
        len(active_assignments),
        latest_run.id,
    )
    if any(assignment.ticket_id == ticket_id for assignment in active_assignments):
        logger.debug(
            "Incremental replanner skipped ticket_id=%s: already present in latest_run_id=%s",
            ticket_id,
            latest_run.id,
        )
        return None

    new_ticket_input = _ticket_to_input(ticket, config)
    if new_ticket_input is None:
        logger.debug("Incremental replanner cannot plan ticket_id=%s: missing/invalid route location", ticket_id)
        return {
            "planned": False,
            "ticket_id": ticket_id,
            "reason": "Ticket has no usable route location.",
        }
    if not any(
        int(technician.id) in available_today_technician_ids
        and new_ticket_input.requirement_codes.issubset(technician.requirement_codes)
        for technician in technicians
    ):
        logger.debug(
            "Incremental replanner cannot plan ticket_id=%s: requirements=%s do not match any technician",
            ticket_id,
            sorted(new_ticket_input.requirement_codes),
        )
        return {
            "planned": False,
            "ticket_id": ticket_id,
            "reason": "No technician has the required skills for this ticket.",
        }

    if new_ticket_input.urgency == TicketUrgency.URGENT:
        logger.debug(
            "Incremental replanner ticket_id=%s is URGENT; removing protected route-work caps for this insert",
            ticket_id,
        )
        # Urgent incremental inserts should not be blocked by the initial
        # planner's protected 6-hour workload target. Keep the real workday and
        # skill constraints, but remove the 6h route-work/non-urgent caps so
        # urgent jobs can be placed today and optionally push normal work out.
        config = replace(
            config,
            initial_non_urgent_minutes_per_technician=24 * 60,
            initial_route_work_minutes_per_technician=24 * 60,
            latest_ticket_start_route_work_minutes=24 * 60,
            latest_ticket_start_penalty_per_minute=0,
            allow_overtime_for_urgent_tickets=True,
        )

    existing_inputs = _ticket_inputs_from_assignments(active_assignments, config)
    logger.debug(
        "Incremental replanner converted %s active assignment(s) to %s existing ticket input(s)",
        len(active_assignments),
        len(existing_inputs),
    )
    ticket_inputs_by_id = {item.id: item for item in existing_inputs}
    ticket_inputs_by_id[new_ticket_input.id] = new_ticket_input
    logger.debug(
        "Incremental replanner building route matrix for ticket_id=%s with %s existing ticket(s) and refresh_cache=%s",
        ticket_id,
        len(existing_inputs),
        config.refresh_route_cache,
    )
    matrix = get_incremental_planning_route_matrix(
        session,
        technicians,
        existing_inputs,
        new_ticket_input,
        refresh_cache=config.refresh_route_cache,
    )
    logger.debug(
        "Incremental replanner matrix ready for ticket_id=%s: %s travel legs",
        ticket_id,
        len(matrix.travel_minutes),
    )

    base_date = config.planned_date.date()
    horizon_days = max(
        1,
        config.planning_horizon_days,
        *((assignment.planned_start_at.date() - base_date).days + 1 for assignment in active_assignments),
    )
    base_routes_by_day = _routes_from_assignments_by_day(
        active_assignments,
        technicians,
        base_date=base_date,
        horizon_days=horizon_days,
    )
    locked_min_positions_by_day = _locked_min_insert_positions_by_day(
        active_assignments,
        technicians,
        base_date=base_date,
        horizon_days=horizon_days,
    )
    original_today = _original_today_assignment_map(active_assignments, base_date)
    logger.debug(
        "Incremental replanner base_date=%s horizon_days=%s original_today_assignments=%s",
        base_date,
        horizon_days,
        len(original_today),
    )

    candidates: list[dict[str, Any]] = []
    target_days = [0] if new_ticket_input.urgency == TicketUrgency.URGENT else list(range(horizon_days))
    logger.debug("Incremental replanner ticket_id=%s trying direct insertion on day indexes=%s", ticket_id, target_days)
    for target_day_index in target_days:
        candidate = _incremental_direct_candidate(
            config=config,
            technicians=technicians,
            matrix=matrix,
            base_routes_by_day=base_routes_by_day,
            ticket_inputs_by_id=ticket_inputs_by_id,
            new_ticket=new_ticket_input,
            target_day_index=target_day_index,
            horizon_days=horizon_days,
            original_today=original_today,
            locked_min_positions_by_day=locked_min_positions_by_day,
            allowed_technician_ids=(available_today_technician_ids if target_day_index == 0 else None),
        )
        if candidate is not None:
            logger.debug(
                "Incremental replanner direct candidate for ticket_id=%s day_index=%s score=%.3f rescheduled_today=%s",
                ticket_id,
                target_day_index,
                candidate["score"],
                len(candidate["rescheduled_today_ticket_ids"]),
            )
            candidates.append(candidate)
        else:
            logger.debug(
                "Incremental replanner direct candidate rejected for ticket_id=%s day_index=%s",
                ticket_id,
                target_day_index,
            )

    # If an urgent ticket cannot fit today directly, move only the minimum
    # non-urgent tail work from the chosen mechanic's same-day route to later
    # days. This avoids the previous combinatorial deferral search.
    today_non_urgent_ids = [
        assignment.ticket_id
        for assignment in active_assignments
        if assignment.planned_start_at.date() == base_date
        and assignment.ticket_id in ticket_inputs_by_id
        and ticket_inputs_by_id[assignment.ticket_id].urgency != TicketUrgency.URGENT
    ]
    if new_ticket_input.urgency == TicketUrgency.URGENT and horizon_days > 1:
        logger.debug(
            "Incremental replanner urgent ticket_id=%s trying minimal same-route tail deferrals/overtime reduction: today_non_urgent=%s",
            ticket_id,
            today_non_urgent_ids,
        )
        tail_candidates = _incremental_minimal_tail_deferral_candidates(
            config=config,
            technicians=technicians,
            matrix=matrix,
            base_routes_by_day=base_routes_by_day,
            ticket_inputs_by_id=ticket_inputs_by_id,
            new_ticket=new_ticket_input,
            horizon_days=horizon_days,
            original_today=original_today,
            locked_min_positions_by_day=locked_min_positions_by_day,
            blocked_day0_technician_ids=unavailable_technician_ids,
        )
        for candidate in tail_candidates:
            logger.debug(
                "Incremental replanner tail-deferral candidate for ticket_id=%s technician_id=%s position=%s moved=%s score=%.3f rescheduled_today=%s",
                ticket_id,
                candidate.get("inserted_technician_id"),
                candidate.get("inserted_position"),
                sorted(candidate.get("moved_ticket_ids", set())),
                candidate["score"],
                len(candidate["rescheduled_today_ticket_ids"]),
            )
            candidates.append(candidate)

    if not candidates:
        logger.debug("Incremental replanner found no feasible candidate for ticket_id=%s", ticket_id)
        return {
            "planned": False,
            "ticket_id": ticket_id,
            "reason": "No feasible incremental insertion found inside the current planning horizon.",
        }

    best = min(candidates, key=lambda candidate: candidate["score"])
    logger.debug(
        "Incremental replanner selected best candidate for ticket_id=%s: score=%.3f new_ticket_day_index=%s rescheduled_today_ids=%s total_candidates=%s",
        ticket_id,
        best["score"],
        best["new_ticket_day_index"],
        sorted(best["rescheduled_today_ticket_ids"]),
        len(candidates),
    )
    planning_run = _persist_incremental_plan(
        session,
        branch_id=ticket.branch_id,
        base_config=config,
        latest_run=latest_run,
        candidate=best,
        new_ticket=new_ticket_input,
    )
    logger.debug(
        "Incremental replanner persisted ticket_id=%s into planning_run_id=%s",
        ticket_id,
        planning_run.id,
    )
    return {
        "planned": True,
        "ticket_id": ticket_id,
        "planning_run_id": planning_run.id,
        "algorithm": "incremental_best_insertion_minimal_tail_deferral",
        "planning_day": (base_date + timedelta(days=best["new_ticket_day_index"])).isoformat(),
        "rescheduled_today_count": len(best["rescheduled_today_ticket_ids"]),
        "rescheduled_today_ticket_ids": sorted(best["rescheduled_today_ticket_ids"]),
        "score": round(best["score"], 3),
        "notes": [
            "Started from the latest persisted plan; did not run the multi-start initial planner.",
            "Inserted the new ticket at the best incremental position without running stochastic/local-search replanning.",
            "For urgent tickets that do not directly fit, only the minimum same-mechanic route tail is moved to later days.",
            "Urgent tickets are only considered for the active planning day; today's non-urgent jobs may be deferred with a soft reschedule penalty.",
            "Medium and low tickets are considered across the horizon with only the existing small medium score tie-breaker.",
        ],
    }


def plan_next_unplanned_ticket_incrementally(
    session: Session,
    payload: dict[str, Any] | None = None,
    *,
    max_candidates: int = 50,
) -> dict[str, Any]:
    """Worker entry point: find one open unplanned ticket and insert it.

    This is deliberately separate from ticket creation and simulator injection.
    The planning worker calls this repeatedly, so API requests only create work
    and the worker is responsible for eventually inserting unplanned tickets into
    the latest active plan.
    """
    payload = payload or {}
    logger.debug("Planning worker scan started: max_candidates=%s payload=%s", max_candidates, payload)
    candidates = list(
        session.scalars(
            select(Ticket)
            .where(Ticket.status == TicketStatus.OPEN)
            .order_by(Ticket.created_at.asc(), Ticket.id.asc())
            .limit(max(1, max_candidates))
        )
    )
    candidates.sort(key=lambda ticket: (0 if ticket.urgency == TicketUrgency.URGENT else 1, ticket.created_at, ticket.id))
    logger.debug(
        "Planning worker scan loaded %s open candidate ticket(s): %s",
        len(candidates),
        [
            {
                "id": ticket.id,
                "branch_id": ticket.branch_id,
                "urgency": getattr(ticket.urgency, "value", str(ticket.urgency)),
                "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
            }
            for ticket in candidates[:10]
        ],
    )

    skipped_planned = 0
    skipped_initial_not_ready = 0
    readiness_by_branch: dict[int, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for ticket in candidates:
        logger.debug(
            "Planning worker checking ticket_id=%s branch_id=%s urgency=%s",
            ticket.id,
            ticket.branch_id,
            ticket.urgency,
        )
        branch_id = int(ticket.branch_id)
        readiness = readiness_by_branch.get(branch_id)
        if readiness is None:
            latest_run = _latest_completed_planning_run(session, branch_id)
            planned_day = (
                latest_run.planned_date.date()
                if latest_run and latest_run.planned_date
                else datetime.utcnow().date()
            )
            readiness = _planning_worker_readiness_for_branch(session, branch_id, planned_day)
            readiness_by_branch[branch_id] = readiness
        if not readiness.get("ready"):
            skipped_initial_not_ready += 1
            logger.debug(
                "Planning worker skipping ticket_id=%s: initial planning not ready for branch_id=%s: %s",
                ticket.id,
                ticket.branch_id,
                readiness,
            )
            continue
        if _ticket_is_in_latest_active_plan(session, ticket):
            skipped_planned += 1
            logger.debug("Planning worker skipping ticket_id=%s: already in latest active plan", ticket.id)
            continue
        logger.debug("Planning worker found unplanned ticket_id=%s; starting incremental replanner", ticket.id)
        result = plan_new_ticket_incrementally(session, ticket.id, payload)
        if result is None:
            skipped_planned += 1
            logger.debug("Planning worker ticket_id=%s returned None from replanner; treating as skipped", ticket.id)
            continue
        if result.get("planned"):
            logger.debug("Planning worker successfully planned ticket_id=%s: %s", ticket.id, result)
            return {
                "checked": len(failures) + skipped_planned + skipped_initial_not_ready + 1,
                "skipped_already_planned": skipped_planned,
                "skipped_initial_not_ready": skipped_initial_not_ready,
                **result,
            }
        logger.debug("Planning worker could not plan ticket_id=%s: %s", ticket.id, result)
        failures.append(result)

    if failures:
        logger.debug(
            "Planning worker scan ended with failures: checked=%s skipped_planned=%s skipped_initial_not_ready=%s failures=%s",
            len(failures) + skipped_planned + skipped_initial_not_ready,
            skipped_planned,
            skipped_initial_not_ready,
            failures[:5],
        )
        return {
            "planned": False,
            "checked": len(failures) + skipped_planned + skipped_initial_not_ready,
            "skipped_already_planned": skipped_planned,
            "skipped_initial_not_ready": skipped_initial_not_ready,
            "reason": "Unplanned ticket(s) were found, but none could be inserted by the incremental replanner.",
            "failures": failures[:5],
        }
    if skipped_initial_not_ready:
        readiness_reasons = [
            readiness for readiness in readiness_by_branch.values() if not readiness.get("ready")
        ]
        logger.debug(
            "Planning worker scan paused: checked=%s skipped_initial_not_ready=%s readiness=%s",
            len(candidates),
            skipped_initial_not_ready,
            readiness_reasons,
        )
        return {
            "planned": False,
            "checked": len(candidates),
            "skipped_already_planned": skipped_planned,
            "skipped_initial_not_ready": skipped_initial_not_ready,
            "reason": "Initial planning is not ready yet; incremental planning is paused.",
            "readiness": readiness_reasons,
        }
    logger.debug(
        "Planning worker scan ended: no open unplanned tickets found. checked=%s skipped_planned=%s",
        len(candidates),
        skipped_planned,
    )
    return {
        "planned": False,
        "checked": len(candidates),
        "skipped_already_planned": skipped_planned,
        "skipped_initial_not_ready": skipped_initial_not_ready,
        "reason": "No open unplanned tickets found.",
    }


def planning_worker_tick(session: Session, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one planning-worker iteration."""
    return plan_next_unplanned_ticket_incrementally(session, payload)


def _ticket_is_in_latest_active_plan(session: Session, ticket: Ticket) -> bool:
    latest_run = _latest_completed_planning_run(session, ticket.branch_id)
    if latest_run is None:
        logger.debug(
            "Latest-plan check for ticket_id=%s branch_id=%s: no completed planning run",
            ticket.id,
            ticket.branch_id,
        )
        return False
    is_planned = (
        session.scalar(
            select(PlanningAssignment.id)
            .where(
                PlanningAssignment.planning_run_id == latest_run.id,
                PlanningAssignment.ticket_id == ticket.id,
                PlanningAssignment.status.in_(VISIBLE_ASSIGNMENT_STATUSES),
            )
            .limit(1)
        )
        is not None
    )
    logger.debug(
        "Latest-plan check for ticket_id=%s branch_id=%s latest_run_id=%s: planned=%s",
        ticket.id,
        ticket.branch_id,
        latest_run.id,
        is_planned,
    )
    return is_planned

def _load_ticket_for_planning(session: Session, ticket_id: int) -> Ticket | None:
    return session.scalar(
        select(Ticket)
        .options(
            joinedload(Ticket.location),
            joinedload(Ticket.subject),
            joinedload(Ticket.ticket_requirements).joinedload(TicketRequirement.requirement),
        )
        .where(Ticket.id == ticket_id)
    )


def _ticket_to_input(ticket: Ticket, config: PlanningConfig) -> TicketInput | None:
    if ticket.location is None or ticket.location.latitude is None or ticket.location.longitude is None:
        return None
    all_requirement_codes = frozenset(
        link.requirement.code.upper()
        for link in ticket.ticket_requirements
        if link.requirement is not None and link.requirement.code is not None
    )
    requirement_codes = frozenset(code for code in all_requirement_codes if code not in SUPPLY_REQUIREMENT_CODES)
    supply_requirement_codes = frozenset(code for code in all_requirement_codes if code in SUPPLY_REQUIREMENT_CODES)
    return TicketInput(
        id=ticket.id,
        location_id=ticket.location_id,
        urgency=ticket.urgency,
        deadline_at=ticket.deadline_at,
        created_at=ticket.created_at,
        service_minutes=ticket.actual_duration_minutes or config.default_service_minutes,
        requirement_codes=requirement_codes,
        supply_requirement_codes=supply_requirement_codes,
        subject=ticket.subject.name if ticket.subject is not None else None,
        address=ticket.location.formatted_address or ticket.location.input_address or "",
    )


def _ticket_inputs_from_assignments(assignments: list[PlanningAssignment], config: PlanningConfig) -> list[TicketInput]:
    result: list[TicketInput] = []
    seen: set[int] = set()
    for assignment in assignments:
        if assignment.ticket_id in seen:
            continue
        item = _ticket_to_input(assignment.ticket, config)
        if item is not None:
            result.append(item)
            seen.add(item.id)
    return result


def _routes_from_assignments_by_day(
    assignments: list[PlanningAssignment],
    technicians: list[Any],
    *,
    base_date: date,
    horizon_days: int,
) -> dict[int, dict[int, list[int]]]:
    routes_by_day: dict[int, dict[int, list[int]]] = {
        day_index: {technician.id: [] for technician in technicians}
        for day_index in range(horizon_days)
    }
    ordered = sorted(assignments, key=lambda item: (item.planned_start_at, item.technician_id, item.sequence_order))
    for assignment in ordered:
        day_index = (assignment.planned_start_at.date() - base_date).days
        if 0 <= day_index < horizon_days and assignment.technician_id in routes_by_day[day_index]:
            routes_by_day[day_index][assignment.technician_id].append(assignment.ticket_id)
    return routes_by_day


def _original_today_assignment_map(assignments: list[PlanningAssignment], base_date: date) -> dict[int, tuple[int, datetime]]:
    return {
        assignment.ticket_id: (assignment.technician_id, assignment.planned_start_at)
        for assignment in assignments
        if assignment.planned_start_at.date() == base_date
    }


def _day_config(config: PlanningConfig, day_index: int) -> PlanningConfig:
    horizon_days = max(1, config.planning_horizon_days)
    if day_index == 0:
        defer_unplanned_penalty_minutes = config.defer_to_day_2_penalty_minutes
    elif day_index == 1:
        defer_unplanned_penalty_minutes = config.defer_to_day_3_penalty_minutes
    else:
        defer_unplanned_penalty_minutes = 0

    return replace(
        config,
        planned_date=config.planned_date + timedelta(days=day_index),
        random_seed=(config.random_seed + day_index if isinstance(config.random_seed, int) else config.random_seed),
        defer_unplanned_penalty_minutes=defer_unplanned_penalty_minutes,
        active_day_travel_penalty_multiplier=(
            max(0.0, config.today_travel_penalty_multiplier) if day_index == 0 else 1.0
        ),
        apply_unplanned_base_penalty=(day_index >= horizon_days - 1),
    )


def _optimizer_from_routes(
    *,
    config: PlanningConfig,
    technicians: list[Any],
    matrix: Any,
    ticket_inputs_by_id: dict[int, TicketInput],
    routes_for_day: dict[int, list[int]],
    extra_ticket_ids: set[int] | None = None,
) -> tuple[InitialRouteOptimizer, PlanningSolution]:
    ticket_ids = set(extra_ticket_ids or set())
    for route_ids in routes_for_day.values():
        ticket_ids.update(route_ids)
    tickets = [ticket_inputs_by_id[ticket_id] for ticket_id in sorted(ticket_ids) if ticket_id in ticket_inputs_by_id]
    optimizer = InitialRouteOptimizer(config=config, technicians=technicians, tickets=tickets, matrix=matrix)
    solution = optimizer._empty_solution()  # noqa: SLF001 - intentional bounded incremental reuse
    for technician_id, route_ids in routes_for_day.items():
        if technician_id in solution.routes:
            solution.routes[technician_id].ticket_ids = [ticket_id for ticket_id in route_ids if ticket_id in ticket_inputs_by_id]
    optimizer._score(solution)  # noqa: SLF001
    return optimizer, solution




def _assignment_is_locked_for_incremental_replanning(assignment: PlanningAssignment) -> bool:
    """Return True when an assignment is operationally immutable.

    Driving and working states are represented by IN_PROGRESS in the current
    backend model.  COMPLETED is also immutable: once a technician has finished
    a stop, an urgent replanning run must not reuse that historical time slot.
    Manual planner locks are treated the same way.
    """
    ticket_status = assignment.ticket.status if assignment.ticket is not None else None
    return bool(
        assignment.locked_by_planner
        or assignment.status in LOCKED_ASSIGNMENT_STATUSES
        or ticket_status in LOCKED_TICKET_STATUSES
    )


def _locked_min_insert_positions_by_day(
    assignments: list[PlanningAssignment],
    technicians: list[Any],
    *,
    base_date: date,
    horizon_days: int,
) -> dict[int, dict[int, int]]:
    """Earliest allowed insertion index per day/technician.

    The value is one past the last locked assignment in that technician's route.
    This preserves the locked route prefix exactly: no new urgent ticket can be
    inserted before a technician is driving to, working on, or has finished a
    ticket, and tail-deferral cannot remove work from that fixed prefix.
    """
    result: dict[int, dict[int, int]] = {
        day_index: {technician.id: 0 for technician in technicians}
        for day_index in range(horizon_days)
    }
    grouped: dict[tuple[int, int], list[PlanningAssignment]] = {}
    for assignment in assignments:
        if assignment.planned_start_at is None:
            continue
        day_index = (assignment.planned_start_at.date() - base_date).days
        if 0 <= day_index < horizon_days:
            grouped.setdefault((day_index, assignment.technician_id), []).append(assignment)

    for (day_index, technician_id), items in grouped.items():
        ordered = sorted(items, key=lambda item: (item.planned_start_at, item.sequence_order, item.id))
        for position, assignment in enumerate(ordered):
            if _assignment_is_locked_for_incremental_replanning(assignment):
                result[day_index][technician_id] = max(result[day_index].get(technician_id, 0), position + 1)
    return result


def _best_locked_safe_insertion(
    optimizer: InitialRouteOptimizer,
    solution: PlanningSolution,
    ticket: TicketInput,
    *,
    min_position_by_technician: dict[int, int],
    technician_ids: list[int] | None = None,
    allow_low_priority: bool,
    allow_non_improving: bool = False,
) -> Any | None:
    """Find the best insertion without touching each route's locked prefix."""
    best = None
    candidate_technician_ids = technician_ids or list(solution.routes)
    for technician_id in candidate_technician_ids:
        route = solution.routes[technician_id]
        if not optimizer._can_do(route.technician, ticket):  # noqa: SLF001
            continue
        min_position = max(0, min_position_by_technician.get(technician_id, 0))
        for position in range(min_position, len(route.ticket_ids) + 1):
            insertion = optimizer._evaluate_insertion(solution, route, ticket, position)  # noqa: SLF001
            if insertion is None:
                continue
            if ticket.is_low_priority and not allow_low_priority:
                continue
            if best is None or insertion.score_delta < best.score_delta:
                best = insertion

    if (
        best is not None
        and not allow_non_improving
        and not optimizer.config.apply_unplanned_base_penalty
        and best.score_delta >= 0
    ):
        return None
    return best


def _incremental_direct_candidate(
    *,
    config: PlanningConfig,
    technicians: list[Any],
    matrix: Any,
    base_routes_by_day: dict[int, dict[int, list[int]]],
    ticket_inputs_by_id: dict[int, TicketInput],
    new_ticket: TicketInput,
    target_day_index: int,
    horizon_days: int,
    original_today: dict[int, tuple[int, datetime]],
    locked_min_positions_by_day: dict[int, dict[int, int]],
    allowed_technician_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    day_results: dict[int, tuple[InitialRouteOptimizer, PlanningSolution]] = {}
    for day_index in range(horizon_days):
        optimizer, solution = _optimizer_from_routes(
            config=_day_config(config, day_index),
            technicians=technicians,
            matrix=matrix,
            ticket_inputs_by_id=ticket_inputs_by_id,
            routes_for_day={tid: ids[:] for tid, ids in base_routes_by_day[day_index].items()},
            extra_ticket_ids={new_ticket.id} if day_index == target_day_index else set(),
        )
        day_results[day_index] = (optimizer, solution)

    optimizer, solution = day_results[target_day_index]
    insertion = _best_locked_safe_insertion(
        optimizer,
        solution,
        new_ticket,
        min_position_by_technician=locked_min_positions_by_day.get(target_day_index, {}),
        technician_ids=allowed_technician_ids,
        allow_low_priority=True,
        allow_non_improving=True,
    )
    if insertion is None:
        # Last-resort urgent policy: an urgent same-day ticket must be planned
        # whenever the route timeline can be built.  The normal insertion helper
        # still uses hard feasibility gates internally; if those gates reject all
        # positions, bypass them for urgent day-0 inserts and let the high
        # overtime/SLA score decide.  This is intentionally limited to urgent
        # tickets on the active day and still respects skills plus the locked
        # prefix, so completed/in-progress work is never moved behind the new job.
        if new_ticket.urgency != TicketUrgency.URGENT or target_day_index != 0:
            return None
        forced = _forced_urgent_overtime_candidate(
            config=config,
            technicians=technicians,
            matrix=matrix,
            base_routes_by_day=base_routes_by_day,
            ticket_inputs_by_id=ticket_inputs_by_id,
            new_ticket=new_ticket,
            horizon_days=horizon_days,
            original_today=original_today,
            locked_min_positions_by_day=locked_min_positions_by_day,
            allowed_technician_ids=allowed_technician_ids,
        )
        if forced is not None:
            forced["forced_urgent_overtime"] = True
        return forced
    solution.routes[insertion.technician_id].ticket_ids.insert(insertion.position, new_ticket.id)
    # Incremental direct insertion intentionally does not run local-search repair.
    # The goal is to keep the existing plan stable and only move the affected
    # route forward after the inserted ticket.
    optimizer._score(solution)  # noqa: SLF001
    day_results[target_day_index] = (optimizer, solution)
    return _incremental_candidate_result(
        config=config,
        day_results=day_results,
        new_ticket_day_index=target_day_index,
        original_today=original_today,
    )



def _forced_urgent_overtime_candidate(
    *,
    config: PlanningConfig,
    technicians: list[Any],
    matrix: Any,
    base_routes_by_day: dict[int, dict[int, list[int]]],
    ticket_inputs_by_id: dict[int, TicketInput],
    new_ticket: TicketInput,
    horizon_days: int,
    original_today: dict[int, tuple[int, datetime]],
    locked_min_positions_by_day: dict[int, dict[int, int]],
    allowed_technician_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    """Build a same-day urgent candidate without day-end hard rejection.

    This is the safety net for late urgent tickets: if the regular insertion
    helper rejects every position, append/insert the urgent ticket after the
    locked prefix on a qualified available technician and score the resulting
    overtime instead of dropping the ticket.
    """
    if new_ticket.urgency != TicketUrgency.URGENT:
        return None

    candidate_technician_ids = set(allowed_technician_ids or [technician.id for technician in technicians])
    best: dict[str, Any] | None = None
    best_meta: tuple[int, int] | None = None
    day0_routes = base_routes_by_day.get(0, {})

    for technician in technicians:
        if technician.id not in candidate_technician_ids:
            continue
        if not new_ticket.requirement_codes.issubset(technician.requirement_codes):
            continue
        original_route = day0_routes.get(technician.id, [])
        min_position = max(0, locked_min_positions_by_day.get(0, {}).get(technician.id, 0))
        for position in range(min_position, len(original_route) + 1):
            day_results: dict[int, tuple[InitialRouteOptimizer, PlanningSolution]] = {}
            for day_index in range(horizon_days):
                routes_for_day = {
                    route_technician_id: route_ids[:]
                    for route_technician_id, route_ids in base_routes_by_day[day_index].items()
                }
                if day_index == 0:
                    routes_for_day.setdefault(technician.id, [])
                    routes_for_day[technician.id].insert(position, new_ticket.id)
                optimizer, solution = _optimizer_from_routes(
                    config=_day_config(config, day_index),
                    technicians=technicians,
                    matrix=matrix,
                    ticket_inputs_by_id=ticket_inputs_by_id,
                    routes_for_day=routes_for_day,
                    extra_ticket_ids={new_ticket.id} if day_index == 0 else set(),
                )
                day_results[day_index] = (optimizer, solution)

            today_optimizer, today_solution = day_results[0]
            today_route = today_solution.routes.get(technician.id)
            if today_route is None:
                continue
            # Keep the one non-negotiable feasibility requirement: the timeline
            # must be constructible.  Overtime and normal route-work limits are
            # represented as score penalties for this urgent fallback.
            if today_optimizer._route_evaluation(today_route).timeline is None:  # noqa: SLF001
                continue

            candidate = _incremental_candidate_result(
                config=config,
                day_results=day_results,
                new_ticket_day_index=0,
                original_today=original_today,
            )
            if best is None or candidate["score"] < best["score"]:
                best = candidate
                best_meta = (technician.id, position)

    if best is not None and best_meta is not None:
        best["inserted_technician_id"] = best_meta[0]
        best["inserted_position"] = best_meta[1]
    return best


def _incremental_minimal_tail_deferral_candidates(
    *,
    config: PlanningConfig,
    technicians: list[Any],
    matrix: Any,
    base_routes_by_day: dict[int, dict[int, list[int]]],
    ticket_inputs_by_id: dict[int, TicketInput],
    new_ticket: TicketInput,
    horizon_days: int,
    original_today: dict[int, tuple[int, datetime]],
    locked_min_positions_by_day: dict[int, dict[int, int]],
    blocked_day0_technician_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Return bounded urgent-insert candidates by pushing only route tail work.

    Stop condition:
    - evaluate each feasible mechanic/position for the urgent ticket on day 0 once;
    - after insertion, remove the smallest possible number of non-urgent tickets
      from the end of that same mechanic's route until day 0 is feasible;
    - greedily insert those removed tickets into later days;
    - no combinations, no full-plan reshuffle, no local search.
    """
    results: list[dict[str, Any]] = []
    day0_routes = base_routes_by_day.get(0, {})
    blocked_day0_technician_ids = blocked_day0_technician_ids or set()
    for technician in technicians:
        if int(technician.id) in blocked_day0_technician_ids:
            continue
        if not new_ticket.requirement_codes.issubset(technician.requirement_codes):
            continue
        original_route = day0_routes.get(technician.id, [])
        min_position = locked_min_positions_by_day.get(0, {}).get(technician.id, 0)
        for position in range(min_position, len(original_route) + 1):
            routes_by_day = {
                day_index: {technician_id: ids[:] for technician_id, ids in routes.items()}
                for day_index, routes in base_routes_by_day.items()
            }
            candidate_route = routes_by_day[0][technician.id]
            candidate_route.insert(position, new_ticket.id)

            optimizer, solution = _optimizer_from_routes(
                config=_day_config(config, 0),
                technicians=technicians,
                matrix=matrix,
                ticket_inputs_by_id=ticket_inputs_by_id,
                routes_for_day=routes_by_day[0],
                extra_ticket_ids={new_ticket.id},
            )

            moved_ticket_ids: list[int] = []
            # Remove only as much non-urgent tail work as needed, and only from
            # the route that received the urgent ticket. For urgent inserts the
            # day-end hard constraint is relaxed, so also remove safe non-urgent
            # tail tickets while doing so reduces overtime. A ticket is safe to
            # push out only when its SLA is not due on the active planning day.
            while True:
                route_stats = optimizer._route_evaluation(solution.routes[technician.id])  # noqa: SLF001
                needs_repair = not optimizer._is_solution_hard_feasible(solution)  # noqa: SLF001
                should_reduce_overtime = route_stats.overtime_minutes > 0
                if not needs_repair and not should_reduce_overtime:
                    break

                route_ids = solution.routes[technician.id].ticket_ids
                removable_index = next(
                    (
                        idx
                        for idx in range(len(route_ids) - 1, -1, -1)
                        if idx >= min_position
                        and route_ids[idx] != new_ticket.id
                        and ticket_inputs_by_id[route_ids[idx]].urgency != TicketUrgency.URGENT
                        and ticket_inputs_by_id[route_ids[idx]].deadline_at.date() > config.planned_date.date()
                    ),
                    None,
                )
                if removable_index is None:
                    break
                moved_ticket_ids.append(route_ids.pop(removable_index))
                optimizer._score(solution)  # noqa: SLF001

            if not optimizer._is_solution_hard_feasible(solution):  # noqa: SLF001
                logger.debug(
                    "Incremental replanner tail candidate rejected: ticket_id=%s technician_id=%s position=%s reason=today_still_infeasible moved=%s",
                    new_ticket.id,
                    technician.id,
                    position,
                    moved_ticket_ids,
                )
                continue

            optimizer._score(solution)  # noqa: SLF001
            day_results: dict[int, tuple[InitialRouteOptimizer, PlanningSolution]] = {0: (optimizer, solution)}
            for day_index in range(1, horizon_days):
                day_results[day_index] = _optimizer_from_routes(
                    config=_day_config(config, day_index),
                    technicians=technicians,
                    matrix=matrix,
                    ticket_inputs_by_id=ticket_inputs_by_id,
                    routes_for_day=routes_by_day[day_index],
                )

            if not _place_moved_tickets_greedily(
                config=config,
                technicians=technicians,
                matrix=matrix,
                ticket_inputs_by_id=ticket_inputs_by_id,
                day_results=day_results,
                moved_ticket_ids=moved_ticket_ids,
                start_day_index=1,
                horizon_days=horizon_days,
                locked_min_positions_by_day=locked_min_positions_by_day,
            ):
                logger.debug(
                    "Incremental replanner tail candidate rejected: ticket_id=%s technician_id=%s position=%s reason=moved_tickets_do_not_fit_future_days moved=%s",
                    new_ticket.id,
                    technician.id,
                    position,
                    moved_ticket_ids,
                )
                continue

            candidate = _incremental_candidate_result(
                config=config,
                day_results=day_results,
                new_ticket_day_index=0,
                original_today=original_today,
            )
            candidate["inserted_technician_id"] = technician.id
            candidate["inserted_position"] = position
            candidate["moved_ticket_ids"] = set(moved_ticket_ids)
            results.append(candidate)
    return results


def _ticket_misses_sla_in_solution(
    optimizer: InitialRouteOptimizer,
    solution: PlanningSolution,
    ticket_id: int,
) -> bool:
    for technician_id in solution.routes:
        for stop in optimizer.build_stops(solution, technician_id):
            if stop.ticket.id == ticket_id:
                return stop.planned_start_at > stop.ticket.deadline_at
    return True


def _place_moved_tickets_greedily(
    *,
    config: PlanningConfig,
    technicians: list[Any],
    matrix: Any,
    ticket_inputs_by_id: dict[int, TicketInput],
    day_results: dict[int, tuple[InitialRouteOptimizer, PlanningSolution]],
    moved_ticket_ids: list[int],
    start_day_index: int,
    horizon_days: int,
    locked_min_positions_by_day: dict[int, dict[int, int]],
) -> bool:
    for moved_ticket_id in moved_ticket_ids:
        moved_ticket = ticket_inputs_by_id[moved_ticket_id]
        best: tuple[float, int, int, int, InitialRouteOptimizer, PlanningSolution] | None = None
        for day_index in range(start_day_index, horizon_days):
            current_optimizer, current_solution = day_results[day_index]
            routes_for_day = {
                technician_id: route.ticket_ids[:]
                for technician_id, route in current_solution.routes.items()
            }
            optimizer, solution = _optimizer_from_routes(
                config=_day_config(config, day_index),
                technicians=technicians,
                matrix=matrix,
                ticket_inputs_by_id=ticket_inputs_by_id,
                routes_for_day=routes_for_day,
                extra_ticket_ids={moved_ticket_id},
            )
            insertion = _best_locked_safe_insertion(
                optimizer,
                solution,
                moved_ticket,
                min_position_by_technician=locked_min_positions_by_day.get(day_index, {}),
                allow_low_priority=True,
                allow_non_improving=True,
            )
            if insertion is None:
                continue
            solution.routes[insertion.technician_id].ticket_ids.insert(insertion.position, moved_ticket_id)
            optimizer._score(solution)  # noqa: SLF001
            if _ticket_misses_sla_in_solution(optimizer, solution, moved_ticket_id):
                continue
            candidate_score = solution.score
            if best is None or candidate_score < best[0]:
                best = (
                    candidate_score,
                    day_index,
                    insertion.technician_id,
                    insertion.position,
                    optimizer,
                    solution,
                )
        if best is None:
            return False
        _, best_day_index, best_technician_id, best_position, best_optimizer, best_solution = best
        logger.debug(
            "Incremental replanner moved ticket_id=%s placed on day_index=%s technician_id=%s position=%s score=%.3f",
            moved_ticket_id,
            best_day_index,
            best_technician_id,
            best_position,
            best_solution.score,
        )
        day_results[best_day_index] = (best_optimizer, best_solution)
    return True


def _incremental_candidate_result(
    *,
    config: PlanningConfig,
    day_results: dict[int, tuple[InitialRouteOptimizer, PlanningSolution]],
    new_ticket_day_index: int,
    original_today: dict[int, tuple[int, datetime]],
) -> dict[str, Any]:
    rescheduled_today_ticket_ids = _rescheduled_today_ticket_ids(day_results.get(0), original_today)
    reschedule_penalty = (
        len(rescheduled_today_ticket_ids)
        * max(0, config.incremental_today_reschedule_penalty_minutes)
        * max(0, config.travel_penalty_per_minute)
    )
    score = sum(solution.score for _, solution in day_results.values()) + reschedule_penalty
    return {
        "day_results": day_results,
        "new_ticket_day_index": new_ticket_day_index,
        "rescheduled_today_ticket_ids": rescheduled_today_ticket_ids,
        "reschedule_penalty": reschedule_penalty,
        "score": score,
    }


def _rescheduled_today_ticket_ids(
    today_result: tuple[InitialRouteOptimizer, PlanningSolution] | None,
    original_today: dict[int, tuple[int, datetime]],
) -> set[int]:
    if today_result is None or not original_today:
        return set()
    optimizer, solution = today_result
    current: dict[int, tuple[int, datetime]] = {}
    for technician_id in solution.routes:
        for stop in optimizer.build_stops(solution, technician_id):
            current[stop.ticket.id] = (technician_id, stop.planned_start_at)
    changed: set[int] = set()
    for ticket_id, original in original_today.items():
        if current.get(ticket_id) != original:
            changed.add(ticket_id)
    return changed


def _persist_incremental_plan(
    session: Session,
    *,
    branch_id: int,
    base_config: PlanningConfig,
    latest_run: PlanningRun,
    candidate: dict[str, Any],
    new_ticket: TicketInput,
) -> PlanningRun:
    logger.debug(
        "Persisting incremental plan for branch_id=%s from latest_run_id=%s new_ticket_id=%s",
        branch_id,
        latest_run.id,
        new_ticket.id,
    )

    # Preserve operational state from the plan we are superseding.  Incremental
    # replanning creates a new planning run containing the full route, so the
    # old visible assignments are marked MOVED for history.  Without carrying
    # these statuses forward, tickets/assignments that are already in progress
    # are recreated as PLANNED and the ticket status is overwritten below.
    previous_assignments = _replanning_base_assignments(session, latest_run.id)
    previous_assignment_by_ticket_id = {
        assignment.ticket_id: assignment
        for assignment in previous_assignments
        if assignment.ticket_id is not None
    }
    previous_assignment_status_by_ticket_id = {
        ticket_id: assignment.status
        for ticket_id, assignment in previous_assignment_by_ticket_id.items()
    }
    previous_ticket_status_by_ticket_id = {
        assignment.ticket_id: assignment.ticket.status
        for assignment in previous_assignments
        if assignment.ticket_id is not None and assignment.ticket is not None
    }

    _move_existing_active_assignments(session, branch_id)
    planning_run = PlanningRun(
        branch_id=branch_id,
        trigger_type=(
            PlanningRunTrigger.NEW_URGENT_TICKET
            if new_ticket.urgency == TicketUrgency.URGENT
            else PlanningRunTrigger.NEW_TICKET
        ),
        status=PlanningRunStatus.RUNNING,
        planned_date=latest_run.planned_date,
        started_at=datetime.utcnow(),
        notes=(
            "Incremental new-ticket insertion from previous planning run "
            f"{latest_run.id}; no multi-start full replanning was executed."
        ),
    )
    session.add(planning_run)
    session.flush()

    planned_ticket_ids: list[int] = []
    sequence_by_technician: dict[int, int] = {}
    total_travel = 0
    total_distance = 0.0
    for day_index in sorted(candidate["day_results"]):
        optimizer, solution = candidate["day_results"][day_index]
        for technician_id, route in solution.routes.items():
            sequence_by_technician.setdefault(technician_id, 1)
            stops = optimizer.build_stops(solution, technician_id)
            for stop in stops:
                previous_assignment = previous_assignment_by_ticket_id.get(stop.ticket.id)
                locked_previous_assignment = (
                    previous_assignment
                    if previous_assignment is not None
                    and _assignment_is_locked_for_incremental_replanning(previous_assignment)
                    else None
                )
                assignment = PlanningAssignment(
                    planning_run_id=planning_run.id,
                    branch_id=branch_id,
                    technician_id=technician_id,
                    ticket_id=stop.ticket.id,
                    sequence_order=sequence_by_technician[technician_id],
                    planned_start_at=(
                        locked_previous_assignment.planned_start_at
                        if locked_previous_assignment is not None
                        else stop.planned_start_at
                    ),
                    planned_end_at=(
                        locked_previous_assignment.planned_end_at
                        if locked_previous_assignment is not None
                        else stop.planned_end_at
                    ),
                    estimated_duration_minutes=(
                        locked_previous_assignment.estimated_duration_minutes
                        if locked_previous_assignment is not None
                        else stop.ticket.service_minutes
                    ),
                    estimated_travel_minutes_before=(
                        locked_previous_assignment.estimated_travel_minutes_before
                        if locked_previous_assignment is not None
                        else stop.travel_minutes_before
                    ),
                    estimated_distance_km_before=(
                        locked_previous_assignment.estimated_distance_km_before
                        if locked_previous_assignment is not None
                        else stop.distance_km_before
                    ),
                    requires_hq_pickup=(
                        locked_previous_assignment.requires_hq_pickup
                        if locked_previous_assignment is not None
                        else stop.requires_hq_pickup
                    ),
                    hq_location_id=(
                        locked_previous_assignment.hq_location_id
                        if locked_previous_assignment is not None
                        else stop.hq_location_id
                    ),
                    estimated_travel_minutes_to_hq=(
                        locked_previous_assignment.estimated_travel_minutes_to_hq
                        if locked_previous_assignment is not None
                        else stop.travel_minutes_to_hq
                    ),
                    estimated_distance_km_to_hq=(
                        locked_previous_assignment.estimated_distance_km_to_hq
                        if locked_previous_assignment is not None
                        else stop.distance_km_to_hq
                    ),
                    estimated_travel_minutes_hq_to_ticket=(
                        locked_previous_assignment.estimated_travel_minutes_hq_to_ticket
                        if locked_previous_assignment is not None
                        else stop.travel_minutes_hq_to_ticket
                    ),
                    estimated_distance_km_hq_to_ticket=(
                        locked_previous_assignment.estimated_distance_km_hq_to_ticket
                        if locked_previous_assignment is not None
                        else stop.distance_km_hq_to_ticket
                    ),
                    status=previous_assignment_status_by_ticket_id.get(
                        stop.ticket.id,
                        PlanningAssignmentStatus.PLANNED,
                    ),
                    source=(
                        locked_previous_assignment.source
                        if locked_previous_assignment is not None
                        else PlanningAssignmentSource.AI
                    ),
                    locked_by_planner=(previous_assignment.locked_by_planner if previous_assignment is not None else False),
                    manual_override_reason=(
                        previous_assignment.manual_override_reason
                        if previous_assignment is not None
                        else None
                    ),
                )
                session.add(assignment)
                planned_ticket_ids.append(stop.ticket.id)
                sequence_by_technician[technician_id] += 1
        total_travel += solution.total_travel_minutes
        total_distance += solution.total_distance_km

    if planned_ticket_ids:
        for planned_ticket in session.query(Ticket).filter(Ticket.id.in_(planned_ticket_ids)).all():
            previous_status = previous_ticket_status_by_ticket_id.get(planned_ticket.id)
            if previous_status in LOCKED_TICKET_STATUSES or previous_status == TicketStatus.CANCELLED:
                planned_ticket.status = previous_status
            elif planned_ticket.status in {TicketStatus.OPEN, TicketStatus.PLANNED}:
                planned_ticket.status = TicketStatus.PLANNED

    planning_run.status = PlanningRunStatus.COMPLETED
    planning_run.completed_at = datetime.utcnow()
    planning_run.score_total_distance_km = round(total_distance, 3)
    planning_run.score_total_travel_minutes = int(total_travel)
    planning_run.score_completed_tickets = len(set(planned_ticket_ids))
    planning_run.score_unplanned_tickets = 0
    session.flush()
    logger.debug(
        "Persisted incremental planning_run_id=%s with %s unique ticket assignment(s), total_travel_minutes=%s, total_distance_km=%.3f",
        planning_run.id,
        len(set(planned_ticket_ids)),
        total_travel,
        total_distance,
    )
    return planning_run


def _minutes_after_midnight(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _assignment_has_started_or_is_immutable_operationally(assignment: PlanningAssignment) -> bool:
    """Return True for work that should define the live technician start state.

    These assignments represent work that already happened or is happening now:
    completed, driving, in-progress, or matching ticket statuses.  Manual
    planner locks are intentionally *not* included here: they are preserved for
    the board, but they should not move the optimizer start location/time or
    otherwise constrain route scoring during operational replanning.
    """
    ticket_status = assignment.ticket.status if assignment.ticket is not None else None
    return bool(
        assignment.status in LOCKED_ASSIGNMENT_STATUSES
        or ticket_status in LOCKED_TICKET_STATUSES
    )


def _assignment_is_unavailable_in_progress(
    assignment: PlanningAssignment,
    unavailable_technician_ids: set[int] | None,
) -> bool:
    """Return True for the one live-work state that must be replanned.

    Normally an in-progress ticket is fixed because the mechanic is already
    working on it. If that mechanic is no longer available, the ticket cannot be
    finished by the same mechanic and must go back into the optimizer.
    """
    if not unavailable_technician_ids or assignment.technician_id not in unavailable_technician_ids:
        return False
    ticket_status = assignment.ticket.status if assignment.ticket is not None else None
    return bool(
        assignment.status == PlanningAssignmentStatus.IN_PROGRESS
        or ticket_status == TicketStatus.IN_PROGRESS
    )


def _operational_daytime_assignments(
    assignments: list[PlanningAssignment],
    base_date: date,
    unavailable_technician_ids: set[int] | None = None,
) -> list[PlanningAssignment]:
    return sorted(
        [
            assignment
            for assignment in assignments
            if assignment.planned_start_at is not None
            and assignment.planned_start_at.date() == base_date
            and _assignment_has_started_or_is_immutable_operationally(assignment)
            and not _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids)
        ],
        key=lambda item: (item.technician_id, item.planned_start_at, item.sequence_order, item.id),
    )


def _preserved_operational_replan_assignments(
    assignments: list[PlanningAssignment],
    config: PlanningConfig,
    unavailable_technician_ids: set[int] | None = None,
) -> list[PlanningAssignment]:
    """Assignments copied into the new run but excluded from optimization.

    This combines the live day work that already happened/is happening with any
    manually locked planning-board assignments across the planning horizon.
    Locked board assignments are kept so the frontend can still display them,
    but they do not participate in the optimizer candidate set.
    """
    base_day = config.planned_date.date()
    horizon_days = max(1, config.planning_horizon_days)
    horizon_end = base_day + timedelta(days=horizon_days)
    preserved: list[PlanningAssignment] = []
    seen_assignment_ids: set[int] = set()
    seen_ticket_ids: set[int] = set()
    for assignment in assignments:
        if assignment.planned_start_at is None:
            continue
        planned_day = assignment.planned_start_at.date()
        in_horizon = base_day <= planned_day < horizon_end
        operationally_fixed = (
            assignment.planned_start_at.date() == base_day
            and _assignment_has_started_or_is_immutable_operationally(assignment)
        )
        planner_locked = in_horizon and assignment.locked_by_planner
        technician_unavailable = bool(
            unavailable_technician_ids is not None
            and assignment.technician_id in unavailable_technician_ids
        )
        unavailable_in_progress = _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids)
        should_preserve = (operationally_fixed and not unavailable_in_progress) or (planner_locked and not technician_unavailable)
        if not should_preserve:
            continue
        if assignment.id in seen_assignment_ids:
            continue
        if assignment.ticket_id is not None and assignment.ticket_id in seen_ticket_ids:
            continue
        seen_assignment_ids.add(assignment.id)
        if assignment.ticket_id is not None:
            seen_ticket_ids.add(assignment.ticket_id)
        preserved.append(assignment)
    return sorted(
        preserved,
        key=lambda item: (item.technician_id, item.planned_start_at, item.sequence_order, item.id),
    )


def _daytime_replan_technicians(
    technicians: list[Any],
    fixed_assignments: list[PlanningAssignment],
) -> list[Any]:
    """Shift each mechanic's day-0 optimizer start behind fixed work.

    Completed, driving and in-progress assignments stay outside the optimizer and
    are copied as-is into the new planning run. The optimizer receives a virtual
    start location/time per mechanic that represents where that mechanic is after
    the fixed prefix, so it plans on top of the live day instead of from 08:00.
    """
    last_fixed_by_technician: dict[int, PlanningAssignment] = {}
    for assignment in fixed_assignments:
        current = last_fixed_by_technician.get(assignment.technician_id)
        if current is None or assignment.planned_end_at > current.planned_end_at:
            last_fixed_by_technician[assignment.technician_id] = assignment

    adjusted = []
    for technician in technicians:
        fixed = last_fixed_by_technician.get(technician.id)
        if fixed is None or fixed.ticket is None or fixed.ticket.location_id is None:
            adjusted.append(technician)
            continue
        adjusted.append(
            replace(
                technician,
                start_location_id=fixed.ticket.location_id,
                workday_start_minutes=max(
                    technician.workday_start_minutes,
                    _minutes_after_midnight(fixed.planned_end_at),
                ),
            )
        )
    return adjusted




def _operational_replan_reference_time(session: Session, config: PlanningConfig) -> datetime:
    """Best effort current time for a mid-day operational replan.

    In simulator-driven scenarios the planning date can be midnight while the
    actual handoff happens later in the simulated day. Use that simulator clock
    when it belongs to the same planning day; otherwise fall back to the
    planning timestamp / wall clock.
    """
    state = session.scalar(select(SimulationState).order_by(SimulationState.id.asc()).limit(1))
    if state is not None and state.current_simulation_time is not None:
        current_time = state.current_simulation_time
        if current_time.date() == config.planned_date.date():
            return current_time

    if config.planned_date is not None and config.planned_date.time() != time.min:
        return config.planned_date
    return datetime.utcnow()


def _promote_unavailable_in_progress_tickets_to_urgent(
    session: Session,
    assignments: list[PlanningAssignment],
    config: PlanningConfig,
    unavailable_technician_ids: set[int] | None,
    *,
    debug_log_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Make abandoned in-progress work genuinely urgent for the optimizer.

    When a mechanic becomes unavailable while working on a ticket, the ticket is
    no longer normal low/medium work: somebody else must pick it up today.  The
    priority change must also refresh deadline_at, otherwise the optimizer keeps
    using the stale low-priority SLA date (or an old urgent date based on the
    original creation time) and can defer the ticket into a later horizon day.
    """
    if not unavailable_technician_ids:
        return []

    reference_time = _operational_replan_reference_time(session, config)
    urgent_deadline = reference_time + timedelta(hours=8)
    promoted: list[dict[str, Any]] = []
    seen_ticket_ids: set[int] = set()

    for assignment in assignments:
        if assignment.ticket_id is None or assignment.ticket is None:
            continue
        if assignment.ticket_id in seen_ticket_ids:
            continue
        if not _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids):
            continue
        if assignment.ticket.status in TERMINAL_TICKET_STATUSES:
            continue

        ticket = assignment.ticket
        old_urgency = _value(ticket.urgency)
        old_deadline = ticket.deadline_at
        ticket.urgency = TicketUrgency.URGENT
        ticket.deadline_at = urgent_deadline
        seen_ticket_ids.add(int(ticket.id))
        promoted.append(
            {
                "ticket_id": int(ticket.id),
                "assignment_id": int(assignment.id),
                "technician_id": int(assignment.technician_id),
                "old_urgency": old_urgency,
                "new_urgency": _value(ticket.urgency),
                "old_deadline_at": _debug_datetime(old_deadline),
                "new_deadline_at": _debug_datetime(ticket.deadline_at),
                "reference_time": _debug_datetime(reference_time),
            }
        )

    if promoted:
        session.flush()
        if debug_log_path is not None:
            _append_planning_debug_log(
                debug_log_path,
                "operational_unavailable_in_progress_tickets_promoted_to_urgent",
                promoted_ticket_count=len(promoted),
                promoted_tickets=promoted,
            )
    return promoted


def _daytime_replan_candidate_tickets(
    session: Session,
    config: PlanningConfig,
    technicians: list[Any],
    base_assignments: list[PlanningAssignment],
    preserved_assignments: list[PlanningAssignment],
    *,
    unavailable_technician_ids: set[int] | None = None,
    debug_log_path: Path | None = None,
) -> list[TicketInput]:
    technician_skill_sets = [technician.requirement_codes for technician in technicians]
    preserved_ticket_ids = {assignment.ticket_id for assignment in preserved_assignments if assignment.ticket_id is not None}
    by_id: dict[int, TicketInput] = {}
    skipped: list[dict[str, Any]] = []
    added_from_previous_plan: list[int] = []
    added_from_open: list[int] = []

    for assignment in base_assignments:
        if assignment.ticket_id in preserved_ticket_ids:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "preserved_ticket_excluded_from_optimizer", "assignment": _debug_assignment_dict(assignment)})
            continue
        if assignment.ticket is None:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "assignment_has_no_ticket", "assignment_id": assignment.id})
            continue
        if assignment.ticket.status in TERMINAL_TICKET_STATUSES:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "terminal_ticket_status", "ticket_status": _value(assignment.ticket.status), "assignment": _debug_assignment_dict(assignment)})
            continue
        unavailable_in_progress = _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids)
        if assignment.status != PlanningAssignmentStatus.PLANNED and not unavailable_in_progress:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "assignment_status_not_planned", "assignment": _debug_assignment_dict(assignment)})
            continue
        item = _ticket_to_input(assignment.ticket, config)
        if item is None:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "ticket_to_input_returned_none", "assignment": _debug_assignment_dict(assignment)})
            continue
        if any(item.requirement_codes.issubset(skills) for skills in technician_skill_sets):
            by_id[item.id] = item
            added_from_previous_plan.append(item.id)
        else:
            skipped.append({"source": "previous_assignment", "ticket_id": assignment.ticket_id, "reason": "no_available_technician_has_required_skills", "ticket": _debug_ticket_input_dict(item)})

    open_tickets = list(
        session.scalars(
            select(Ticket)
            .options(
                joinedload(Ticket.location),
                joinedload(Ticket.subject),
                joinedload(Ticket.ticket_requirements).joinedload(TicketRequirement.requirement),
            )
            .where(Ticket.branch_id == config.branch_id, Ticket.status == TicketStatus.OPEN)
            .order_by(Ticket.created_at.asc(), Ticket.id.asc())
        ).unique().all()
    )
    for ticket in open_tickets:
        if ticket.id in preserved_ticket_ids:
            skipped.append({"source": "open_ticket_query", "ticket_id": ticket.id, "reason": "preserved_ticket_excluded_from_optimizer"})
            continue
        item = _ticket_to_input(ticket, config)
        if item is None:
            skipped.append({"source": "open_ticket_query", "ticket_id": ticket.id, "reason": "ticket_to_input_returned_none"})
            continue
        if any(item.requirement_codes.issubset(skills) for skills in technician_skill_sets):
            by_id[item.id] = item
            added_from_open.append(item.id)
        else:
            skipped.append({"source": "open_ticket_query", "ticket_id": ticket.id, "reason": "no_available_technician_has_required_skills", "ticket": _debug_ticket_input_dict(item)})

    result = list(by_id.values())
    result.sort(key=lambda ticket: (ticket.urgency_rank, ticket.created_at, ticket.id))
    if debug_log_path is not None:
        _append_planning_debug_log(
            debug_log_path,
            "operational_candidate_ticket_selection",
            previous_assignment_count=len(base_assignments),
            preserved_ticket_ids=sorted(ticket_id for ticket_id in preserved_ticket_ids if ticket_id is not None),
            open_ticket_query_count=len(open_tickets),
            added_from_previous_plan=added_from_previous_plan,
            added_from_open=added_from_open,
            final_candidate_ticket_ids=[ticket.id for ticket in result],
            final_candidate_tickets=[_debug_ticket_input_dict(ticket) for ticket in result],
            skipped_count=len(skipped),
            skipped=skipped,
        )
    return result


def _build_daytime_replan_horizon_plan(
    config: PlanningConfig,
    technicians: list[Any],
    day0_technicians: list[Any],
    tickets: list[Any],
    matrix: Any,
    *,
    planning_run_id: int | None = None,
    debug_log_path: Path | None = None,
) -> list[dict[str, Any]]:
    remaining_by_id = {ticket.id: ticket for ticket in tickets}
    day_plans: list[dict[str, Any]] = []
    for day_index in range(max(1, config.planning_horizon_days)):
        if not remaining_by_id and day_index > 0:
            break
        day_config = _day_config(config, day_index)
        day_technicians = day0_technicians if day_index == 0 else technicians
        day_tickets = list(remaining_by_id.values())
        if debug_log_path is not None:
            _append_planning_debug_log(
                debug_log_path,
                "operational_horizon_day_started",
                planning_run_id=planning_run_id,
                day_index=day_index,
                planned_date=day_config.planned_date.date().isoformat(),
                remaining_ticket_ids=sorted(remaining_by_id),
                remaining_ticket_count=len(remaining_by_id),
                technician_states=[_debug_technician_input_dict(technician) for technician in day_technicians],
                apply_unplanned_base_penalty=day_config.apply_unplanned_base_penalty,
                defer_unplanned_penalty_minutes=day_config.defer_unplanned_penalty_minutes,
                active_day_travel_penalty_multiplier=day_config.active_day_travel_penalty_multiplier,
            )
        optimizer = InitialRouteOptimizer(
            config=day_config,
            technicians=day_technicians,
            tickets=day_tickets,
            matrix=matrix,
            debug_log_path=debug_log_path,
            debug_label=(
                f"operational_replan planning_run_id={planning_run_id} "
                f"day_index={day_index} date={day_config.planned_date.date().isoformat()}"
            ),
        )
        solution = optimizer.optimize()
        stops_by_technician = {
            technician_id: optimizer.build_stops(solution, technician_id)
            for technician_id in solution.routes
        }
        planned_ids = {
            stop.ticket.id
            for stops in stops_by_technician.values()
            for stop in stops
        }
        if debug_log_path is not None:
            _append_planning_debug_log(
                debug_log_path,
                "operational_horizon_day_finished",
                planning_run_id=planning_run_id,
                day_index=day_index,
                planned_date=day_config.planned_date.date().isoformat(),
                planned_ticket_ids=sorted(planned_ids),
                unplanned_ticket_ids=sorted(set(remaining_by_id) - planned_ids),
                solution_unplanned_ticket_ids=sorted(solution.unplanned_ticket_ids),
                route_ticket_ids={technician_id: list(route.ticket_ids) for technician_id, route in solution.routes.items()},
                stop_windows={
                    technician_id: [
                        {
                            "ticket_id": stop.ticket.id,
                            "start": stop.planned_start_at.isoformat(),
                            "end": stop.planned_end_at.isoformat(),
                            "travel_before": stop.travel_minutes_before,
                            "distance_before": stop.distance_km_before,
                        }
                        for stop in stops
                    ]
                    for technician_id, stops in stops_by_technician.items()
                },
                total_travel_minutes=solution.total_travel_minutes,
                total_distance_km=solution.total_distance_km,
                score=solution.score,
            )
        day_plans.append(
            {
                "day_index": day_index,
                "config": day_config,
                "optimizer": optimizer,
                "solution": solution,
                "tickets": day_tickets,
                "planned_ticket_ids": planned_ids,
            }
        )
        for ticket_id in planned_ids:
            remaining_by_id.pop(ticket_id, None)
    return day_plans


def _copy_assignment_for_new_run(
    assignment: PlanningAssignment,
    *,
    planning_run_id: int,
    sequence_order: int,
) -> PlanningAssignment:
    return PlanningAssignment(
        planning_run_id=planning_run_id,
        branch_id=assignment.branch_id,
        technician_id=assignment.technician_id,
        ticket_id=assignment.ticket_id,
        sequence_order=sequence_order,
        planned_start_at=assignment.planned_start_at,
        planned_end_at=assignment.planned_end_at,
        estimated_duration_minutes=assignment.estimated_duration_minutes,
        estimated_travel_minutes_before=assignment.estimated_travel_minutes_before,
        estimated_distance_km_before=assignment.estimated_distance_km_before,
        requires_hq_pickup=assignment.requires_hq_pickup,
        hq_location_id=assignment.hq_location_id,
        estimated_travel_minutes_to_hq=assignment.estimated_travel_minutes_to_hq,
        estimated_distance_km_to_hq=assignment.estimated_distance_km_to_hq,
        estimated_travel_minutes_hq_to_ticket=assignment.estimated_travel_minutes_hq_to_ticket,
        estimated_distance_km_hq_to_ticket=assignment.estimated_distance_km_hq_to_ticket,
        status=assignment.status,
        source=assignment.source,
        locked_by_planner=assignment.locked_by_planner,
        manual_override_reason=assignment.manual_override_reason,
    )



def _parse_planning_date(value: str | date | datetime | None, fallback: datetime | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    return (fallback or datetime.utcnow()).date()


def _availability_map(session: Session, branch_id: int, planned_day: date) -> dict[int, bool]:
    rows = session.scalars(
        select(TechnicianAvailability).where(
            TechnicianAvailability.branch_id == branch_id,
            TechnicianAvailability.available_date == planned_day,
        )
    ).all()
    return {int(row.technician_id): bool(row.is_available) for row in rows}


def _unavailable_technician_ids_for_date(session: Session, branch_id: int, planned_day: date) -> set[int]:
    return {
        int(technician_id)
        for technician_id, is_available in _availability_map(session, branch_id, planned_day).items()
        if not is_available
    }


def _filter_technicians_available_on_date(
    session: Session,
    technicians: list[Any],
    *,
    branch_id: int,
    planned_day: date,
) -> list[Any]:
    unavailable_ids = _unavailable_technician_ids_for_date(session, branch_id, planned_day)
    if not unavailable_ids:
        return technicians
    return [technician for technician in technicians if int(technician.id) not in unavailable_ids]


def _require_available_technicians(technicians: list[Any], *, branch_id: int, planned_day: date) -> list[Any]:
    if not technicians:
        raise PlanningAiError(
            f"No available technicians found for branch {branch_id} on {planned_day.isoformat()}"
        )
    return technicians


def set_technician_availability(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    branch_id = int(payload.get("branch_id") or 1)
    technician_id = int(payload["technician_id"])
    planned_day = _parse_planning_date(payload.get("planned_date") or payload.get("available_date"))
    is_available = bool(payload.get("is_available"))

    technician = session.get(Technician, technician_id)
    if technician is None or int(technician.branch_id) != branch_id:
        raise PlanningAiError(f"Technician {technician_id} was not found for branch {branch_id}")

    existing = session.scalar(
        select(TechnicianAvailability).where(
            TechnicianAvailability.branch_id == branch_id,
            TechnicianAvailability.technician_id == technician_id,
            TechnicianAvailability.available_date == planned_day,
        )
    )
    if existing is None:
        existing = TechnicianAvailability(
            branch_id=branch_id,
            technician_id=technician_id,
            available_date=planned_day,
            is_available=is_available,
        )
        session.add(existing)
    else:
        existing.is_available = is_available
    session.flush()
    return {
        "branch_id": branch_id,
        "technician_id": technician_id,
        "planned_date": planned_day.isoformat(),
        "is_available": is_available,
    }

def _active_technician_ids_from_payload(payload: dict[str, Any]) -> set[int] | None:
    raw_ids = payload.get("active_technician_ids")
    if raw_ids is None:
        return None
    if not isinstance(raw_ids, list):
        raise PlanningAiError("active_technician_ids must be a list of technician IDs")
    ids = {int(value) for value in raw_ids if value is not None}
    if not ids:
        raise PlanningAiError("Select at least one technician for replanning")
    return ids


def _filter_active_replan_technicians(technicians: list[Any], active_technician_ids: set[int] | None) -> list[Any]:
    if active_technician_ids is None:
        return technicians
    selected = [technician for technician in technicians if int(technician.id) in active_technician_ids]
    if not selected:
        available_ids = ", ".join(str(technician.id) for technician in technicians) or "none"
        raise PlanningAiError(
            f"None of the selected technicians are available for this branch. Available technician IDs: {available_ids}"
        )
    return selected


def run_operational_replanning(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Replan the active day around fixed, already-started work.

    Unlike the overnight initial planner, this is intended for mid-day replans:
    completed, driving and in-progress stops keep their original assignment
    records in the new run, and remaining work is optimized from each mechanic's
    current post-fixed location/time. Routing is read from route_cache only.
    """
    config = _config_from_payload(payload)
    if _active_planning_run(session, config.branch_id) is not None:
        active_run = _active_planning_run(session, config.branch_id)
        raise ActivePlanningRunError(
            f"Planning run {active_run.id} is already {_value(active_run.status).lower()} for branch {config.branch_id}."
        )
    latest_run = _latest_completed_planning_run(session, config.branch_id)
    if latest_run is None:
        raise PlanningAiError("Operational replanning needs an existing completed planning run to preserve fixed work.")

    base_assignments = _replanning_base_assignments(session, latest_run.id)
    base_date = config.planned_date.date()
    active_technician_ids = _active_technician_ids_from_payload(payload)
    all_technicians = load_available_technicians(session, config)
    if active_technician_ids is not None:
        all_ids = {int(technician.id) for technician in all_technicians}
        for technician in all_technicians:
            set_technician_availability(session, {
                "branch_id": config.branch_id,
                "technician_id": int(technician.id),
                "planned_date": base_date,
                "is_available": int(technician.id) in active_technician_ids,
            })
        unavailable_technician_ids = all_ids - active_technician_ids
    else:
        unavailable_technician_ids = _unavailable_technician_ids_for_date(session, config.branch_id, base_date)
        active_technician_ids = {int(technician.id) for technician in all_technicians} - unavailable_technician_ids
    technicians = _filter_active_replan_technicians(all_technicians, active_technician_ids)
    operational_fixed_assignments = _operational_daytime_assignments(
        base_assignments,
        base_date,
        unavailable_technician_ids=unavailable_technician_ids,
    )
    preserved_assignments = _preserved_operational_replan_assignments(
        base_assignments,
        config,
        unavailable_technician_ids=unavailable_technician_ids,
    )

    planning_run: PlanningRun | None = None
    try:
        planning_run = _create_visible_planning_run(
            session,
            branch_id=config.branch_id,
            trigger_type=PlanningRunTrigger.PLANNER_INTERVENTION,
            planned_date=config.planned_date,
            notes=(
                f"Operational daytime replanning from previous planning run {latest_run.id}. "
                "Completed, driving and in-progress assignments were preserved; route matrix was loaded from DB only."
            ),
        )
        debug_log_path = _planning_debug_log_path(planning_run.id)
        _append_planning_debug_log(
            debug_log_path,
            "operational_replanning_debug_log_started",
            planning_run_id=planning_run.id,
            previous_planning_run_id=latest_run.id,
            requested_config={
                "branch_id": config.branch_id,
                "planned_date": config.planned_date.isoformat(),
                "base_date": base_date.isoformat(),
                "planning_horizon_days": config.planning_horizon_days,
                "initial_route_work_minutes_per_technician": config.initial_route_work_minutes_per_technician,
                "latest_ticket_start_route_work_minutes": config.latest_ticket_start_route_work_minutes,
                "latest_ticket_start_penalty_per_minute": config.latest_ticket_start_penalty_per_minute,
                "today_travel_penalty_multiplier": config.today_travel_penalty_multiplier,
                "defer_to_day_2_penalty_minutes": config.defer_to_day_2_penalty_minutes,
                "defer_to_day_3_penalty_minutes": config.defer_to_day_3_penalty_minutes,
                "multi_start_iterations": config.multi_start_iterations,
                "local_search_iterations": config.local_search_iterations,
                "random_seed": config.random_seed,
                "active_technician_ids": sorted(active_technician_ids) if active_technician_ids is not None else None,
                "unavailable_technician_ids": sorted(unavailable_technician_ids) if unavailable_technician_ids is not None else None,
            },
            selected_technicians=[_debug_technician_input_dict(technician) for technician in technicians],
            unavailable_technician_ids=sorted(unavailable_technician_ids) if unavailable_technician_ids is not None else [],
            previous_assignment_count=len(base_assignments),
            previous_assignments=[_debug_assignment_dict(assignment) for assignment in base_assignments],
            operational_fixed_assignment_count=len(operational_fixed_assignments),
            operational_fixed_assignment_ids=[assignment.id for assignment in operational_fixed_assignments],
            operational_fixed_assignments=[_debug_assignment_dict(assignment) for assignment in operational_fixed_assignments],
            preserved_assignment_count=len(preserved_assignments),
            preserved_assignment_ids=[assignment.id for assignment in preserved_assignments],
            preserved_assignments=[_debug_assignment_dict(assignment) for assignment in preserved_assignments],
            preserved_locked_assignment_ids=[assignment.id for assignment in preserved_assignments if assignment.locked_by_planner],
            loaded_technicians=[_debug_technician_input_dict(technician) for technician in technicians],
        )

        _promote_unavailable_in_progress_tickets_to_urgent(
            session,
            base_assignments,
            config,
            unavailable_technician_ids,
            debug_log_path=debug_log_path,
        )

        day0_technicians = _daytime_replan_technicians(technicians, operational_fixed_assignments)
        _append_planning_debug_log(
            debug_log_path,
            "operational_day0_technician_start_states",
            original_technicians=[_debug_technician_input_dict(technician) for technician in technicians],
            adjusted_day0_technicians=[_debug_technician_input_dict(technician) for technician in day0_technicians],
        )

        tickets = _daytime_replan_candidate_tickets(
            session,
            config,
            technicians,
            base_assignments,
            preserved_assignments,
            unavailable_technician_ids=unavailable_technician_ids,
            debug_log_path=debug_log_path,
        )
        _append_planning_debug_log(
            debug_log_path,
            "operational_route_matrix_load_started",
            technician_count=len(technicians),
            day0_technician_count=len(day0_technicians),
            ticket_count=len(tickets),
            ticket_location_ids=sorted({ticket.location_id for ticket in tickets}),
            technician_location_ids=sorted({
                *[technician.start_location_id for technician in technicians],
                *[technician.end_location_id for technician in technicians],
                *[technician.office_location_id for technician in technicians],
                *[technician.start_location_id for technician in day0_technicians],
                *[technician.end_location_id for technician in day0_technicians],
                *[technician.office_location_id for technician in day0_technicians],
            }),
        )
        matrix = get_cached_planning_route_matrix(session, [*technicians, *day0_technicians], tickets)
        _append_planning_debug_log(
            debug_log_path,
            "operational_route_matrix_load_finished",
            travel_pair_count=len(matrix.travel_minutes),
            distance_pair_count=len(matrix.distance_km),
            sample_travel_pairs=[
                {"from": from_id, "to": to_id, "minutes": minutes}
                for (from_id, to_id), minutes in list(matrix.travel_minutes.items())[:25]
            ],
        )

        day_plans = _build_daytime_replan_horizon_plan(
            config,
            technicians,
            day0_technicians,
            tickets,
            matrix,
            planning_run_id=planning_run.id,
            debug_log_path=debug_log_path,
        )

        planned_ticket_ids: list[int] = []
        previous_assignment_by_ticket_id = {assignment.ticket_id: assignment for assignment in base_assignments}
        previous_ticket_status_by_ticket_id = {
            assignment.ticket_id: assignment.ticket.status
            for assignment in base_assignments
            if assignment.ticket_id is not None and assignment.ticket is not None
        }
        replanned_unavailable_in_progress_ticket_ids = {
            assignment.ticket_id
            for assignment in base_assignments
            if assignment.ticket_id is not None
            and _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids)
        }
        sequence_by_technician: dict[int, int] = {technician.id: 1 for technician in technicians}
        created_assignments: list[PlanningAssignment] = []

        for assignment in preserved_assignments:
            copied = _copy_assignment_for_new_run(
                assignment,
                planning_run_id=planning_run.id,
                sequence_order=sequence_by_technician.get(assignment.technician_id, 1),
            )
            session.add(copied)
            created_assignments.append(copied)
            if assignment.ticket_id is not None:
                planned_ticket_ids.append(assignment.ticket_id)
            sequence_by_technician[assignment.technician_id] = sequence_by_technician.get(assignment.technician_id, 1) + 1

        for day_plan in day_plans:
            optimizer: InitialRouteOptimizer = day_plan["optimizer"]
            solution: PlanningSolution = day_plan["solution"]
            for technician_id, route in solution.routes.items():
                sequence_by_technician.setdefault(technician_id, 1)
                for stop in optimizer.build_stops(solution, technician_id):
                    previous_assignment = previous_assignment_by_ticket_id.get(stop.ticket.id)
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
                        locked_by_planner=(previous_assignment.locked_by_planner if previous_assignment is not None else False),
                        manual_override_reason=(previous_assignment.manual_override_reason if previous_assignment is not None else None),
                    )
                    session.add(assignment)
                    created_assignments.append(assignment)
                    planned_ticket_ids.append(stop.ticket.id)
                    sequence_by_technician[technician_id] += 1

        # Re-sequence after all preserved and optimized assignments have been
        # created so the board sorts by actual time, not by copy/optimization
        # order. This is important when a locked assignment for tomorrow is
        # copied before newly optimized same-technician work for that day.
        for technician_id in {assignment.technician_id for assignment in created_assignments}:
            technician_items = sorted(
                [assignment for assignment in created_assignments if assignment.technician_id == technician_id],
                key=lambda item: (
                    item.planned_start_at or datetime.max,
                    item.planned_end_at or datetime.max,
                    item.sequence_order,
                    item.ticket_id or 0,
                ),
            )
            for sequence_order, assignment in enumerate(technician_items, start=1):
                assignment.sequence_order = sequence_order

        # Supersede the previous visible plan only after the replacement rows
        # have been created.  The preserved assignment list contains live ORM
        # objects from the previous run; marking the previous run as MOVED before
        # copying them mutates those objects and causes the copied rows to be
        # persisted as MOVED as well.  MOVED rows are intentionally hidden from
        # the planning overview, which made completed/driving/in-progress locked
        # tickets disappear after replanning.
        _move_existing_active_assignments(
            session,
            config.branch_id,
            exclude_planning_run_id=planning_run.id,
        )

        _append_planning_debug_log(
            debug_log_path,
            "operational_preserved_assignments_copied",
            planning_run_id=planning_run.id,
            preserved_assignment_count=len(preserved_assignments),
            preserved_assignments=[_debug_assignment_dict(assignment) for assignment in preserved_assignments],
            created_assignment_count=len(created_assignments),
            created_assignment_ticket_ids=[assignment.ticket_id for assignment in created_assignments],
        )

        planned_ticket_id_set = set(planned_ticket_ids)
        if planned_ticket_ids:
            for ticket in session.query(Ticket).filter(Ticket.id.in_(planned_ticket_ids)).all():
                previous_status = previous_ticket_status_by_ticket_id.get(ticket.id)
                if ticket.id in replanned_unavailable_in_progress_ticket_ids:
                    ticket.status = TicketStatus.PLANNED
                elif previous_status in LOCKED_TICKET_STATUSES or previous_status == TicketStatus.CANCELLED:
                    ticket.status = previous_status
                elif ticket.status in {TicketStatus.OPEN, TicketStatus.PLANNED}:
                    ticket.status = TicketStatus.PLANNED

        # Tickets that were removed from an unavailable technician and could not
        # be placed with a selected technician must not remain silently planned
        # without a visible assignment. Return them to OPEN so they stay visible
        # as unplanned work after the old assignment rows are marked MOVED.
        preserved_ticket_ids = {assignment.ticket_id for assignment in preserved_assignments if assignment.ticket_id is not None}
        previous_replan_ticket_ids = {
            assignment.ticket_id
            for assignment in base_assignments
            if assignment.ticket_id is not None
            and assignment.ticket_id not in preserved_ticket_ids
            and (
                assignment.status == PlanningAssignmentStatus.PLANNED
                or _assignment_is_unavailable_in_progress(assignment, unavailable_technician_ids)
            )
            and assignment.ticket is not None
            and assignment.ticket.status not in TERMINAL_TICKET_STATUSES
        }
        unplanned_previous_ticket_ids = previous_replan_ticket_ids - planned_ticket_id_set
        if unplanned_previous_ticket_ids:
            for ticket in session.query(Ticket).filter(Ticket.id.in_(unplanned_previous_ticket_ids)).all():
                if ticket.status in {TicketStatus.PLANNED, TicketStatus.IN_PROGRESS}:
                    ticket.status = TicketStatus.OPEN

        summary = _horizon_summary(day_plans, tickets)
        planning_run.status = PlanningRunStatus.COMPLETED
        planning_run.completed_at = datetime.utcnow()
        planning_run.score_total_distance_km = round(summary["total_distance_km"], 3)
        planning_run.score_total_travel_minutes = int(summary["total_travel_minutes"])
        planning_run.score_completed_tickets = len(set(planned_ticket_ids))
        planning_run.score_unplanned_tickets = int(summary["unplanned_tickets"])
        _append_planning_debug_log(
            debug_log_path,
            "operational_replanning_debug_log_finished",
            planning_run_id=planning_run.id,
            status=_value(planning_run.status),
            summary=summary,
        )
        result = _horizon_solution_as_dict(config, day_plans)
        result["planning_run_id"] = planning_run.id
        result["planning_run_status"] = _value(planning_run.status)
        result["debug_log_path"] = str(debug_log_path)
        result["operational_fixed_assignment_count"] = len(operational_fixed_assignments)
        result["preserved_assignment_count"] = len(preserved_assignments)
        result["overview"] = get_planning_overview(session, branch_id=config.branch_id)
        return result
    except Exception as exc:
        _mark_planning_run_failed(session, planning_run, exc)
        raise

def run_replanning(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a new plan and mark previous active assignments as moved.

    Existing assignments are kept for history, but no longer appear as active on
    tickets once the new plan is generated.
    """
    branch_id = int(payload.get("branch_id") or 1)
    if _active_planning_run(session, branch_id) is not None:
        active_run = _active_planning_run(session, branch_id)
        raise ActivePlanningRunError(
            f"Planning run {active_run.id} is already {_value(active_run.status).lower()} for branch {branch_id}."
        )
    latest_run = _latest_completed_planning_run(session, branch_id)
    base_assignments = _replanning_base_assignments(session, latest_run.id if latest_run else None)
    if any(_assignment_is_locked_for_incremental_replanning(assignment) for assignment in base_assignments):
        return run_operational_replanning(session, payload)

    _move_existing_active_assignments(session, branch_id)
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


def _latest_completed_planning_run_for_day(
    session: Session,
    branch_id: int,
    planned_day: date,
) -> PlanningRun | None:
    runs = session.scalars(
        select(PlanningRun)
        .where(
            PlanningRun.branch_id == branch_id,
            PlanningRun.status == PlanningRunStatus.COMPLETED,
        )
        .order_by(PlanningRun.completed_at.desc().nullslast(), PlanningRun.id.desc())
    ).all()
    return next((run for run in runs if run.planned_date and run.planned_date.date() == planned_day), None)


def _latest_completed_initial_planning_run_for_day(
    session: Session,
    branch_id: int,
    planned_day: date,
) -> PlanningRun | None:
    runs = session.scalars(
        select(PlanningRun)
        .where(
            PlanningRun.branch_id == branch_id,
            PlanningRun.status == PlanningRunStatus.COMPLETED,
            PlanningRun.trigger_type == PlanningRunTrigger.DAILY_START,
        )
        .order_by(PlanningRun.completed_at.desc().nullslast(), PlanningRun.id.desc())
    ).all()
    return next((run for run in runs if run.planned_date and run.planned_date.date() == planned_day), None)


def _active_ticket_count_for_branch(session: Session, branch_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.branch_id == branch_id,
                Ticket.status.in_(list(PLANNING_WORKER_ACTIVE_TICKET_STATUSES)),
            )
        )
        or 0
    )


def _current_ticket_assignment_count_for_run(session: Session, planning_run_id: int, branch_id: int) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(PlanningAssignment.ticket_id)))
            .join(Ticket, Ticket.id == PlanningAssignment.ticket_id)
            .where(
                PlanningAssignment.planning_run_id == planning_run_id,
                PlanningAssignment.branch_id == branch_id,
                PlanningAssignment.status.in_(VISIBLE_ASSIGNMENT_STATUSES),
                Ticket.branch_id == branch_id,
                Ticket.status.in_(list(PLANNING_WORKER_ACTIVE_TICKET_STATUSES)),
            )
        )
        or 0
    )


def _planning_worker_readiness_for_branch(
    session: Session,
    branch_id: int,
    planned_day: date,
) -> dict[str, Any]:
    latest_run = _latest_completed_planning_run_for_day(session, branch_id, planned_day)
    if latest_run is None:
        return {
            "ready": False,
            "branch_id": branch_id,
            "planned_date": planned_day.isoformat(),
            "reason": "No completed planning run exists for this branch and planning day yet.",
        }

    initial_run = _latest_completed_initial_planning_run_for_day(session, branch_id, planned_day)
    if initial_run is None:
        return {
            "ready": False,
            "branch_id": branch_id,
            "planned_date": planned_day.isoformat(),
            "latest_planning_run_id": latest_run.id,
            "latest_trigger_type": _value(latest_run.trigger_type),
            "reason": "No completed DAILY_START initial planning run exists for this branch and planning day yet.",
        }

    active_ticket_count = _active_ticket_count_for_branch(session, branch_id)
    current_assignment_count = _current_ticket_assignment_count_for_run(session, latest_run.id, branch_id)
    coverage = 1.0 if active_ticket_count == 0 else current_assignment_count / active_ticket_count

    # Scenario regeneration removes simulator-generated tickets and their dependent
    # planning assignments, but it intentionally leaves planning_runs for history.
    # The completed DAILY_START run is the definitive signal, while this coverage
    # check prevents a stale historical run with no current assignments from
    # unlocking the incremental single-ticket planner for a newly generated day.
    if active_ticket_count and coverage < PLANNING_WORKER_MIN_INITIAL_PLAN_COVERAGE:
        empty_initial_run = (
            latest_run.id == initial_run.id
            and int(latest_run.score_completed_tickets or 0) == 0
            and int(latest_run.score_unplanned_tickets or 0) == 0
        )
        if not empty_initial_run:
            return {
                "ready": False,
                "branch_id": branch_id,
                "planned_date": planned_day.isoformat(),
                "initial_planning_run_id": initial_run.id,
                "latest_planning_run_id": latest_run.id,
                "latest_trigger_type": _value(latest_run.trigger_type),
                "active_ticket_count": active_ticket_count,
                "current_assignment_count": current_assignment_count,
                "coverage": round(coverage, 3),
                "minimum_coverage": PLANNING_WORKER_MIN_INITIAL_PLAN_COVERAGE,
                "reason": (
                    "A completed initial planning run exists, but the latest visible plan covers "
                    "too few current tickets. Treating it as stale until initial planning is run again."
                ),
            }

    return {
        "ready": True,
        "branch_id": branch_id,
        "planned_date": planned_day.isoformat(),
        "initial_planning_run_id": initial_run.id,
        "latest_planning_run_id": latest_run.id,
        "latest_trigger_type": _value(latest_run.trigger_type),
        "active_ticket_count": active_ticket_count,
        "current_assignment_count": current_assignment_count,
        "coverage": round(coverage, 3),
    }


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




def _replanning_base_assignments(session: Session, planning_run_id: int | None) -> list[PlanningAssignment]:
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
                PlanningAssignment.status.in_(list(REPLANNING_BASE_ASSIGNMENT_STATUSES)),
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
        end_location_id = technician.end_location.id if technician.end_location is not None else None
        if previous_location_id is not None and end_location_id is not None:
            pairs.add((previous_location_id, end_location_id))

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


def _return_travel_from_assignments(
    technicians: list[Technician],
    assignments_by_technician: dict[int, list[PlanningAssignment]],
    route_lookup: dict[tuple[int, int], tuple[int, float]],
) -> tuple[int, float]:
    travel_minutes = 0
    distance_km = 0.0
    for technician in technicians:
        assignments = assignments_by_technician.get(technician.id, [])
        if not assignments:
            continue
        last_assignment = assignments[-1]
        from_location_id = last_assignment.ticket.location_id
        end_location = technician.end_location
        to_location_id = end_location.id if end_location is not None else None
        if from_location_id is None or to_location_id is None:
            continue
        leg = route_lookup.get((from_location_id, to_location_id))
        if leg is None:
            continue
        minutes, distance = leg
        travel_minutes += int(minutes)
        distance_km += float(distance)
    return travel_minutes, distance_km


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

    if assignments and planned_date is not None:
        end_location = technician.end_location
        end_location_id = end_location.id if end_location is not None else None
        if previous_location_id is not None and end_location_id is not None:
            return_leg = (route_lookup or {}).get((previous_location_id, end_location_id))
            if return_leg is not None:
                return_minutes, return_distance = return_leg
                return_start = previous_end
                return_end = return_start + timedelta(minutes=return_minutes)
                if not break_inserted:
                    break_item = _break_item_between(
                        technician.id,
                        previous_end,
                        return_start,
                        planned_date=planned_date,
                    )
                    if break_item is not None:
                        items.append(break_item)
                        break_inserted = True
                if return_minutes > 0:
                    items.append(
                        _travel_item(
                            f"travel-return-home-{technician.id}-{planned_date.strftime('%Y%m%d')}",
                            return_start,
                            return_end,
                            return_minutes,
                            return_distance,
                            from_location_id=previous_location_id,
                            to_location_id=end_location_id,
                        )
                    )
                previous_end = return_end
                previous_location_id = end_location_id

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


def _move_existing_active_assignments(
    session: Session,
    branch_id: int,
    *,
    exclude_planning_run_id: int | None = None,
) -> None:
    criteria = [
        PlanningAssignment.branch_id == branch_id,
        PlanningAssignment.status.in_(list(VISIBLE_ASSIGNMENT_STATUSES)),
    ]
    if exclude_planning_run_id is not None:
        criteria.append(PlanningAssignment.planning_run_id != exclude_planning_run_id)

    assignments = session.scalars(
        select(PlanningAssignment).where(*criteria)
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
    technicians = _require_available_technicians(
        _filter_technicians_available_on_date(
            session,
            load_available_technicians(session, config),
            branch_id=config.branch_id,
            planned_day=config.planned_date.date(),
        ),
        branch_id=config.branch_id,
        planned_day=config.planned_date.date(),
    )
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
    planning_run: PlanningRun | None = None
    try:
        planning_run = _create_visible_planning_run(
            session,
            branch_id=config.branch_id,
            trigger_type=PlanningRunTrigger.DAILY_START,
            planned_date=config.planned_date,
            notes=(
                "Initial planning generated for a multi-day horizon by multi-start "
                "randomized cheapest insertion + local search."
            ),
        )
        debug_log_path = _planning_debug_log_path(planning_run.id)
        _append_planning_debug_log(
            debug_log_path,
            "planning_run_debug_log_started",
            planning_run_id=planning_run.id,
            branch_id=config.branch_id,
            planned_date=config.planned_date.isoformat(),
            file_format="json_lines",
            description=(
                "Verbose planner diagnostics. Each line is one JSON object showing why "
                "candidate plans and insertions were accepted, rejected, or selected."
            ),
        )
        technicians = _require_available_technicians(
            _filter_technicians_available_on_date(
                session,
                load_available_technicians(session, config),
                branch_id=config.branch_id,
                planned_day=config.planned_date.date(),
            ),
            branch_id=config.branch_id,
            planned_day=config.planned_date.date(),
        )
        tickets = load_candidate_tickets(session, config, technicians)
        matrix = get_planning_route_matrix(
            session,
            technicians,
            tickets,
            refresh_cache=config.refresh_route_cache,
        )
        day_plans = _build_horizon_plan(
            config,
            technicians,
            tickets,
            matrix,
            planning_run_id=planning_run.id,
            debug_log_path=debug_log_path,
        )

        planned_ticket_ids: list[int] = []
        created_assignments: list[PlanningAssignment] = []
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
                    created_assignments.append(assignment)
                    planned_ticket_ids.append(stop.ticket.id)
                    sequence_by_technician[technician_id] += 1

        # Re-sequence after all preserved and optimized assignments have been
        # created so the board sorts by actual time, not by copy/optimization
        # order. This is important when a locked assignment for tomorrow is
        # copied before newly optimized same-technician work for that day.
        for technician_id in {assignment.technician_id for assignment in created_assignments}:
            technician_items = sorted(
                [assignment for assignment in created_assignments if assignment.technician_id == technician_id],
                key=lambda item: (
                    item.planned_start_at or datetime.max,
                    item.planned_end_at or datetime.max,
                    item.sequence_order,
                    item.ticket_id or 0,
                ),
            )
            for sequence_order, assignment in enumerate(technician_items, start=1):
                assignment.sequence_order = sequence_order

        _append_planning_debug_log(
            debug_log_path,
            "planning_assignments_persisted",
            planning_run_id=planning_run.id,
            created_assignment_count=len(created_assignments),
            created_assignment_ticket_ids=[assignment.ticket_id for assignment in created_assignments],
        )

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
        _append_planning_debug_log(
            debug_log_path,
            "planning_run_debug_log_finished",
            planning_run_id=planning_run.id,
            status=_value(planning_run.status),
            summary=summary,
        )

        result = _horizon_solution_as_dict(config, day_plans)
        result["planning_run_id"] = planning_run.id
        result["planning_run_status"] = _value(planning_run.status)
        result["debug_log_path"] = str(debug_log_path)
        return result
    except Exception as exc:
        _mark_planning_run_failed(session, planning_run, exc)
        raise


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
        route_work_overflow_grace_minutes=int(
            payload.get("route_work_overflow_grace_minutes") or 15
        ),
        latest_ticket_start_route_work_minutes=int(
            payload.get("latest_ticket_start_route_work_minutes") or 360
        ),
        latest_ticket_start_penalty_per_minute=int(
            payload.get("latest_ticket_start_penalty_per_minute") or 40
        ),
        travel_penalty_per_minute=int(payload.get("travel_penalty_per_minute") or 25),
        today_travel_penalty_multiplier=float(
            payload.get("today_travel_penalty_multiplier")
            if payload.get("today_travel_penalty_multiplier") is not None
            else 5.0
        ),
        planning_horizon_days=max(1, int(payload.get("planning_horizon_days") or 3)),
        defer_to_day_2_penalty_minutes=int(
            payload.get("defer_to_day_2_penalty_minutes") or 50
        ),
        defer_to_day_3_penalty_minutes=int(
            payload.get("defer_to_day_3_penalty_minutes") or 120
        ),
        default_service_minutes=int(payload.get("default_service_minutes") or 60),
        multi_start_iterations=int(payload.get("multi_start_iterations") or 10),
        local_search_iterations=int(payload.get("local_search_iterations") or 60),
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
        incremental_today_reschedule_penalty_minutes=int(
            payload.get("incremental_today_reschedule_penalty_minutes") or 30
        ),
    )


def _build_horizon_plan(
    config: PlanningConfig,
    technicians: list[Any],
    tickets: list[Any],
    matrix: Any,
    *,
    planning_run_id: int | None = None,
    debug_log_path: Path | None = None,
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

        day_config = _day_config(config, day_index)
        if debug_log_path is not None:
            _append_planning_debug_log(
                debug_log_path,
                "planning_day_started",
                planning_run_id=planning_run_id,
                day_index=day_index,
                planning_date=day_config.planned_date.date().isoformat(),
                remaining_ticket_count=len(remaining_by_id),
                travel_penalty_per_minute=day_config.travel_penalty_per_minute,
                active_day_travel_multiplier=day_config.active_day_travel_penalty_multiplier,
                effective_travel_penalty_per_minute=(
                    max(0, day_config.travel_penalty_per_minute)
                    * max(0.0, day_config.active_day_travel_penalty_multiplier)
                ),
                defer_unplanned_penalty_minutes=day_config.defer_unplanned_penalty_minutes,
                apply_unplanned_base_penalty=day_config.apply_unplanned_base_penalty,
            )
        remaining_tickets = sorted(
            remaining_by_id.values(),
            key=lambda ticket: (
                ticket.urgency_rank,
                ticket.created_at,
                ticket.id,
            ),
        )
        optimizer = InitialRouteOptimizer(
            config=day_config,
            technicians=technicians,
            tickets=remaining_tickets,
            matrix=matrix,
            debug_log_path=debug_log_path,
            debug_label=(
                f"planning_run_id={planning_run_id} day_index={day_index} "
                f"date={day_config.planned_date.date().isoformat()}"
            ),
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
        if debug_log_path is not None:
            _append_planning_debug_log(
                debug_log_path,
                "planning_day_finished",
                planning_run_id=planning_run_id,
                day_index=day_index,
                planning_date=day_config.planned_date.date().isoformat(),
                total_cost_for_selected_day_planning=solution.score,
                planned_ticket_ids=sorted(planned_ids),
                remaining_ticket_ids=sorted(remaining_by_id),
                total_travel_minutes=solution.total_travel_minutes,
                total_distance_km=round(solution.total_distance_km, 3),
                completed_tickets=solution.completed_tickets,
                unplanned_ticket_ids=sorted(solution.unplanned_ticket_ids),
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
        "route_work_overflow_grace_minutes": config.route_work_overflow_grace_minutes,
        "latest_ticket_start_route_work_minutes": config.latest_ticket_start_route_work_minutes,
        "latest_ticket_start_penalty_per_minute": config.latest_ticket_start_penalty_per_minute,
        "reserved_urgent_minutes_per_technician_per_day": max(
            0, 8 * 60 - config.initial_route_work_minutes_per_technician
        ),
        "travel_penalty_per_minute": config.travel_penalty_per_minute,
        "today_travel_penalty_multiplier": config.today_travel_penalty_multiplier,
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
            "route_work_overflow_grace_minutes": config.route_work_overflow_grace_minutes,
            "latest_ticket_start_route_work_minutes": (
                config.latest_ticket_start_route_work_minutes
            ),
            "latest_ticket_start_penalty_per_minute": (
                config.latest_ticket_start_penalty_per_minute
            ),
            "travel_penalty_per_minute": config.travel_penalty_per_minute,
            "active_day_travel_penalty_multiplier": config.active_day_travel_penalty_multiplier,
            "effective_travel_penalty_per_minute": (
                config.travel_penalty_per_minute * config.active_day_travel_penalty_multiplier
            ),
            "defer_unplanned_penalty_minutes": config.defer_unplanned_penalty_minutes,
            "penalty_weights": {
                "sla_miss": SLA_MISS_PENALTY,
                "unplanned_base_per_ticket": (UNPLANNED_TICKET_PENALTY if config.apply_unplanned_base_penalty else 0),
                "unplanned_urgent_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.URGENT],
                "unplanned_medium_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.MEDIUM],
                "unplanned_low_tiebreaker": UNPLANNED_URGENCY_TIEBREAKER[TicketUrgency.LOW],
                "overtime_per_minute": OVERTIME_PENALTY_PER_MINUTE,
                "travel_per_minute": config.travel_penalty_per_minute,
                "active_day_travel_multiplier": config.active_day_travel_penalty_multiplier,
                "effective_travel_per_minute": (
                    config.travel_penalty_per_minute * config.active_day_travel_penalty_multiplier
                ),
                "defer_unplanned_per_ticket": (
                    config.defer_unplanned_penalty_minutes * config.travel_penalty_per_minute
                ),
                "apply_unplanned_base_penalty": config.apply_unplanned_base_penalty,
            },
        },
        "design_choices": [
            "Multiple randomized start plans are tried to avoid all nearby-home mechanics staying in the same area.",
            "Each start plan is improved with move, swap and reorder operations.",
            "Non-final horizon days score leftovers as deferred work; only the final horizon day applies the true unplanned-ticket base penalty.",
            "Every mechanic gets a 45 minute break planned inside the 11:00-13:00 window.",
            "A route with one or more supply requirements gets one HQ pickup before the first supply ticket.",
            "Travel and break blocks are returned as explicit timeline items, instead of appearing as gaps between tickets.",
            "Today's driving minutes are weighted more heavily than future-day driving minutes; default today multiplier is 5x.",
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
