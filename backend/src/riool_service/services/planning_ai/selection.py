from __future__ import annotations

from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.models.technician import Technician, TechnicianStatus
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.services.planning_ai.models import PlanningConfig, TechnicianInput, TicketInput


class PlanningSelectionError(ValueError):
    pass


def load_available_technicians(session: Session, config: PlanningConfig) -> list[TechnicianInput]:
    technicians = list(
        session.execute(
            select(Technician)
            .options(
                joinedload(Technician.home_location),
                joinedload(Technician.branch),
                joinedload(Technician.technician_requirements).joinedload(TechnicianRequirement.requirement),
            )
            .where(
                Technician.branch_id == config.branch_id,
                Technician.status == TechnicianStatus.ACTIVE,
            )
            .order_by(Technician.id)
        )
        .unique()
        .scalars()
    )
    if not technicians:
        raise PlanningSelectionError(f"No active technicians found for branch {config.branch_id}")

    result: list[TechnicianInput] = []
    for technician in technicians:
        start_location = technician.start_location
        end_location = technician.end_location
        if start_location is None or end_location is None:
            continue
        if start_location.latitude is None or start_location.longitude is None:
            continue
        requirement_codes = frozenset(
            req.requirement.code for req in technician.technician_requirements if req.requirement is not None
        )
        result.append(
            TechnicianInput(
                id=technician.id,
                name=technician.name,
                start_location_id=start_location.id,
                end_location_id=end_location.id,
                workday_start_minutes=technician.workday_start_minutes,
                workday_end_minutes=technician.workday_end_minutes,
                requirement_codes=requirement_codes,
            )
        )

    if not result:
        raise PlanningSelectionError("No active technicians with usable start locations were found")
    return result


def load_candidate_tickets(
    session: Session,
    config: PlanningConfig,
    technicians: list[TechnicianInput],
) -> list[TicketInput]:
    max_candidates = min(95 - len({t.start_location_id for t in technicians}), max(1, len(technicians) * config.max_candidates_per_technician))
    if max_candidates <= 0:
        raise PlanningSelectionError("Too many technician start locations for one OSRM planning matrix")

    technician_skill_sets = [technician.requirement_codes for technician in technicians]

    # Broad load first, then rank in Python so we can use both SLA and planning-specific logic.
    tickets = list(
        session.execute(
            select(Ticket)
            .options(
                joinedload(Ticket.location),
                joinedload(Ticket.subject),
                joinedload(Ticket.ticket_requirements).joinedload(TicketRequirement.requirement),
            )
            .where(
                Ticket.branch_id == config.branch_id,
                Ticket.status == TicketStatus.OPEN,
            )
            .order_by(Ticket.deadline_at.asc(), Ticket.created_at.asc(), Ticket.id.asc())
            .limit(max_candidates * 3)
        )
        .unique()
        .scalars()
    )

    candidates: list[TicketInput] = []
    for ticket in tickets:
        if ticket.location is None or ticket.location.latitude is None or ticket.location.longitude is None:
            continue
        requirement_codes = frozenset(
            req.requirement.code for req in ticket.ticket_requirements if req.requirement is not None
        )
        if not any(requirement_codes.issubset(skill_set) for skill_set in technician_skill_sets):
            continue
        candidates.append(
            TicketInput(
                id=ticket.id,
                location_id=ticket.location_id,
                urgency=ticket.urgency,
                deadline_at=ticket.deadline_at,
                created_at=ticket.created_at,
                service_minutes=ticket.actual_duration_minutes or config.default_service_minutes,
                requirement_codes=requirement_codes,
                subject=ticket.subject.name if ticket.subject is not None else None,
                address=ticket.location.formatted_address or ticket.location.input_address or "",
            )
        )

    candidates.sort(key=_candidate_sort_key)
    return candidates[:max_candidates]


def planning_day_start(config: PlanningConfig, technician: TechnicianInput) -> datetime:
    return _datetime_at_minutes(config.planned_date, technician.workday_start_minutes)


def planning_day_end(config: PlanningConfig, technician: TechnicianInput) -> datetime:
    return _datetime_at_minutes(config.planned_date, technician.workday_end_minutes)


def _datetime_at_minutes(anchor: datetime, minutes_after_midnight: int) -> datetime:
    return datetime.combine(anchor.date(), time.min, tzinfo=anchor.tzinfo).replace(
        hour=minutes_after_midnight // 60,
        minute=minutes_after_midnight % 60,
    )


def _candidate_sort_key(ticket: TicketInput) -> tuple[int, datetime, int, datetime, int]:
    # Urgent and earliest deadlines are protected first. More constrained tickets
    # are inserted earlier so they are not boxed out by flexible work.
    return (
        ticket.urgency_rank,
        ticket.deadline_at,
        -len(ticket.requirement_codes),
        ticket.created_at,
        ticket.id,
    )
