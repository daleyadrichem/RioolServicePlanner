from __future__ import annotations

from dataclasses import asdict
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.models.simulation_tickets import SimulationTicket
from riool_service.database.models.tickets import Ticket, TicketStatus
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.simulator.config import ScenarioConfig, TICKET_SCENARIOS_CONFIG_ENV_VAR, load_scenarios
from riool_service.simulator.fill_simulation_tickets import seed_simulation_tickets
from riool_service.simulator.fill_tickets import seed_tickets
from riool_service.simulator.utils import deadline_for, parse_time

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "ticket_scenarios_config.json"

# Lightweight in-memory state for the demo simulator controls.
# The planned tickets themselves stay in the database.
SIMULATOR_STATE: dict[str, Any] = {
    "scenario_id": "normale_dag",
    "simulation_date": date.today().isoformat(),
    "current_time": "08:00",
    "speed": 5,
    "status": "Gepauzeerd",
    "activity_log": [],
}


def _scenario_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def _scenario_to_dict(scenario: ScenarioConfig) -> dict[str, Any]:
    return {
        "id": scenario.scenario_id,
        "name": scenario.name,
        "branch_name": scenario.branch_name,
        "day_start_time": scenario.day_start_time,
        "day_end_time": scenario.day_end_time,
        "percentage_urgent": scenario.percentage_urgent,
        "percentage_mid_prio": scenario.percentage_mid_prio,
        "percentage_low_prio": scenario.percentage_low_prio,
    }


def list_scenarios() -> list[dict[str, Any]]:
    scenarios = load_scenarios(_scenario_config_path())
    return [_scenario_to_dict(scenario) for scenario in scenarios.values()]


def get_scenario_or_raise(scenario_id: str) -> ScenarioConfig:
    scenarios = load_scenarios(_scenario_config_path())
    if scenario_id not in scenarios:
        available = ", ".join(sorted(scenarios))
        raise ValueError(f"Unknown scenario_id {scenario_id!r}. Available: {available}")
    return scenarios[scenario_id]


def generate_scenario_tickets(scenario_id: str, seed: int | None = None) -> dict[str, Any]:
    scenario = get_scenario_or_raise(scenario_id)
    os.environ[TICKET_SCENARIOS_CONFIG_ENV_VAR] = str(_scenario_config_path())
    day = date.fromisoformat(str(SIMULATOR_STATE.get("simulation_date") or date.today().isoformat()))

    normal_result = seed_tickets(scenario.scenario_id, simulation_date=day, seed=seed)
    simulation_result = seed_simulation_tickets(
        scenario.scenario_id,
        simulation_date=day,
        seed=None if seed is None else seed + 1,
    )

    SIMULATOR_STATE.update(
        {
            "scenario_id": scenario.scenario_id,
            "current_time": scenario.day_start_time,
            "status": "Gepauzeerd",
            "activity_log": [
                {
                    "time": scenario.day_start_time,
                    "message": f"Scenario '{scenario.name}' gegenereerd",
                    "actor": "Simulator",
                }
            ],
        }
    )

    return {
        "scenario": _scenario_to_dict(scenario),
        "tickets": asdict(normal_result),
        "simulation_tickets": asdict(simulation_result),
    }


def _format_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else ""


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _requirement_codes(ticket: SimulationTicket) -> set[str]:
    return {
        link.requirement.code.upper()
        for link in ticket.ticket_requirements
        if link.requirement is not None and link.requirement.code is not None
    }


def _ticket_address(ticket: SimulationTicket) -> str:
    location = ticket.location
    if not location:
        return ""
    return location.formatted_address or location.input_address or location.city or ""


def list_planned_injections(session: Session) -> list[dict[str, Any]]:
    tickets = session.scalars(
        select(SimulationTicket)
        .options(
            joinedload(SimulationTicket.subject),
            joinedload(SimulationTicket.location),
            joinedload(SimulationTicket.ticket_requirements).joinedload(TicketRequirement.requirement),
        )
        .order_by(SimulationTicket.created_at.asc(), SimulationTicket.id.asc())
    ).unique().all()

    rows: list[dict[str, Any]] = []
    for ticket in tickets:
        requirements = _requirement_codes(ticket)
        rows.append(
            {
                "inject_time": _format_time(ticket.created_at),
                "inject_at": ticket.created_at.isoformat(),
                "id": f"SIM-{ticket.id:03d}",
                "database_id": ticket.id,
                "urgency": _value(ticket.urgency),
                "requires_ladder": "LADDER" in requirements,
                "requires_spring": "VEER" in requirements,
                "requirements": sorted(requirements),
                "subject": ticket.subject.name if ticket.subject else "Onbekend",
                "address": _ticket_address(ticket),
            }
        )
    return rows


def _current_datetime(scenario: ScenarioConfig) -> datetime:
    sim_date = date.fromisoformat(str(SIMULATOR_STATE.get("simulation_date") or date.today().isoformat()))
    current_time = parse_time(str(SIMULATOR_STATE.get("current_time") or scenario.day_start_time))
    return datetime.combine(sim_date, current_time)


def inject_due_tickets(session: Session) -> int:
    scenario = get_scenario_or_raise(str(SIMULATOR_STATE.get("scenario_id") or "normale_dag"))
    current_dt = _current_datetime(scenario)

    due_tickets = session.scalars(
        select(SimulationTicket)
        .options(
            joinedload(SimulationTicket.ticket_requirements).joinedload(TicketRequirement.requirement)
        )
        .where(SimulationTicket.created_at <= current_dt)
        .order_by(SimulationTicket.created_at.asc(), SimulationTicket.id.asc())
    ).unique().all()

    injected_count = 0
    for simulation_ticket in due_tickets:
        ticket = Ticket(
            branch_id=simulation_ticket.branch_id,
            location_id=simulation_ticket.location_id,
            subject_id=simulation_ticket.subject_id,
            description=simulation_ticket.description,
            urgency=simulation_ticket.urgency,
            status=TicketStatus.OPEN,
            created_at=simulation_ticket.created_at,
            deadline_at=deadline_for(simulation_ticket.created_at, simulation_ticket.urgency),
        )
        session.add(ticket)
        session.flush()

        for requirement_link in list(simulation_ticket.ticket_requirements):
            session.add(
                TicketRequirement(
                    ticket_id=ticket.id,
                    simulation_ticket_id=None,
                    requirement_id=requirement_link.requirement_id,
                )
            )

        session.delete(simulation_ticket)
        injected_count += 1

    if injected_count:
        session.flush()
        log = list(SIMULATOR_STATE.get("activity_log") or [])
        log.insert(
            0,
            {
                "time": str(SIMULATOR_STATE.get("current_time") or scenario.day_start_time),
                "message": f"{injected_count} ticket(s) ingeschoten",
                "actor": "Simulator",
            },
        )
        SIMULATOR_STATE["activity_log"] = log[:10]
    return injected_count


def get_state(session: Session) -> dict[str, Any]:
    scenario = get_scenario_or_raise(str(SIMULATOR_STATE.get("scenario_id") or "normale_dag"))
    not_injected = session.scalar(select(func.count(SimulationTicket.id))) or 0
    injected_today = len(SIMULATOR_STATE.get("activity_log") or [])
    return {
        "scenario_id": scenario.scenario_id,
        "scenario": scenario.name,
        "current_time": SIMULATOR_STATE.get("current_time") or scenario.day_start_time,
        "speed": SIMULATOR_STATE.get("speed", 5),
        "status": SIMULATOR_STATE.get("status", "Gepauzeerd"),
        "stats": {
            "tickets_in_scenario": not_injected,
            "not_injected": not_injected,
            "injected_today": injected_today,
            "last_injection": (SIMULATOR_STATE.get("activity_log") or [{}])[0].get("time", "–") if SIMULATOR_STATE.get("activity_log") else "–",
        },
        "activity_log": SIMULATOR_STATE.get("activity_log") or [],
    }


def start() -> dict[str, str]:
    SIMULATOR_STATE["status"] = "Actief"
    return {"status": "Actief"}


def pause() -> dict[str, str]:
    SIMULATOR_STATE["status"] = "Gepauzeerd"
    return {"status": "Gepauzeerd"}


def reset(session: Session) -> dict[str, Any]:
    scenario = get_scenario_or_raise(str(SIMULATOR_STATE.get("scenario_id") or "normale_dag"))
    SIMULATOR_STATE.update(
        {"current_time": scenario.day_start_time, "status": "Gepauzeerd", "activity_log": []}
    )
    return get_state(session)


def step(session: Session, minutes: int = 15) -> dict[str, Any]:
    scenario = get_scenario_or_raise(str(SIMULATOR_STATE.get("scenario_id") or "normale_dag"))
    current_dt = _current_datetime(scenario) + timedelta(minutes=minutes)
    end_time = parse_time(scenario.day_end_time)
    if current_dt.time() > end_time:
        current_dt = datetime.combine(current_dt.date(), end_time)
    SIMULATOR_STATE["current_time"] = current_dt.strftime("%H:%M")
    injected = inject_due_tickets(session)
    return {"injected_count": injected, "state": get_state(session)}


def delete_injection(session: Session, injection_id: int) -> dict[str, Any]:
    ticket = session.get(SimulationTicket, injection_id)
    if ticket is None:
        raise ValueError(f"Simulation ticket {injection_id} was not found")
    session.delete(ticket)
    return {"deleted": True, "id": injection_id}
