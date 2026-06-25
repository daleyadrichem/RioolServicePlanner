from __future__ import annotations

from dataclasses import asdict
import logging
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.models.base import Base
from riool_service.database.models.simulation_state import SimulationState, SimulationStatus
from riool_service.database.models.simulation_tickets import SimulationTicket
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.db_utils import get_engine
from riool_service.simulator.config import ScenarioConfig, TICKET_SCENARIOS_CONFIG_ENV_VAR, load_scenarios
from riool_service.simulator.fill_simulation_tickets import seed_simulation_tickets
from riool_service.simulator.fill_tickets import seed_tickets
from riool_service.simulator.db_helpers import add_requirement_links, get_branch_by_name, get_or_create_subject
from riool_service.simulator.utils import deadline_for, parse_time
from riool_service.geocode_service import coordinates_from_address

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "ticket_scenarios_config.json"
DEFAULT_STATE_ID = 1
DEFAULT_SPEED = 5
MAX_ACTIVITY_ITEMS = 20

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    SimulationStatus.IDLE: "Gepauzeerd",
    SimulationStatus.RUNNING: "Actief",
    SimulationStatus.PAUSED: "Gepauzeerd",
    SimulationStatus.STOPPED: "Gestopt",
    SimulationStatus.COMPLETED: "Afgerond",
}


class SimulationIsRunningError(RuntimeError):
    """Raised when simulator injections are edited while the worker may be running."""


class SimulationTicketNotFoundError(ValueError):
    """Raised when a requested simulation ticket does not exist."""


def ensure_simulator_tables() -> None:
    """Create simulator tables that may not exist yet in older local databases."""
    Base.metadata.create_all(get_engine())


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


def _day_dt(simulation_day: date, hhmm: str) -> datetime:
    return datetime.combine(simulation_day, parse_time(hhmm))


def _new_state_for_scenario(scenario: ScenarioConfig, simulation_day: date) -> SimulationState:
    return SimulationState(
        id=DEFAULT_STATE_ID,
        scenario_id=scenario.scenario_id,
        status=SimulationStatus.PAUSED,
        simulation_date=simulation_day,
        current_simulation_time=_day_dt(simulation_day, scenario.day_start_time),
        day_start_at=_day_dt(simulation_day, scenario.day_start_time),
        day_end_at=_day_dt(simulation_day, scenario.day_end_time),
        speed_multiplier=DEFAULT_SPEED,
        last_tick_real_time=None,
        activity_log=[
            {
                "time": scenario.day_start_time,
                "message": f"Scenario '{scenario.name}' gegenereerd",
                "actor": "Simulator",
            }
        ],
    )


def get_or_create_state(session: Session) -> SimulationState:
    ensure_simulator_tables()
    state = session.get(SimulationState, DEFAULT_STATE_ID)
    if state is not None:
        return state

    scenario = get_scenario_or_raise("normale_dag")
    state = _new_state_for_scenario(scenario, date.today())
    state.status = SimulationStatus.IDLE
    state.activity_log = []
    session.add(state)
    session.flush()
    return state


def _append_log(state: SimulationState, message: str, actor: str = "Simulator", at: datetime | None = None) -> None:
    when = at or state.current_simulation_time
    current = list(state.activity_log or [])
    current.insert(
        0,
        {
            "time": when.strftime("%H:%M"),
            "message": message,
            "actor": actor,
        },
    )
    state.activity_log = current[:MAX_ACTIVITY_ITEMS]


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


def _format_location_address(street: str | None, house_number: str | None, city: str | None) -> str:
    if street and house_number and city:
        return f"{street} {house_number}, {city}"
    return ""


def _strip_country_from_address(address: str) -> str:
    parts = [part.strip() for part in str(address or "").split(",") if part.strip()]
    if len(parts) > 2:
        return ", ".join(parts[:2])
    return str(address or "").strip()


def _ticket_address(ticket: SimulationTicket) -> str:
    location = ticket.location
    if not location:
        return ""
    formatted = _format_location_address(location.street, location.house_number, location.city)
    if formatted:
        return formatted
    return _strip_country_from_address(location.formatted_address or location.input_address or location.city or "")


def generate_scenario_tickets(session: Session, scenario_id: str, seed: int | None = None) -> dict[str, Any]:
    state_before_generation = get_or_create_state(session)
    if state_before_generation.status == SimulationStatus.RUNNING:
        raise SimulationIsRunningError("Scenario tickets can only be generated when the simulation is paused or stopped.")
    scenario = get_scenario_or_raise(scenario_id)
    os.environ[TICKET_SCENARIOS_CONFIG_ENV_VAR] = str(_scenario_config_path())
    simulation_day = date.today()

    # The existing seeders own their own DB sessions, so commit any API work first.
    session.commit()
    normal_result = seed_tickets(scenario.scenario_id, simulation_date=simulation_day, seed=seed)
    simulation_result = seed_simulation_tickets(
        scenario.scenario_id,
        simulation_date=simulation_day,
        seed=None if seed is None else seed + 1,
    )

    state = session.get(SimulationState, DEFAULT_STATE_ID)
    if state is None:
        state = _new_state_for_scenario(scenario, simulation_day)
        session.add(state)
    else:
        fresh = _new_state_for_scenario(scenario, simulation_day)
        state.scenario_id = fresh.scenario_id
        state.status = fresh.status
        state.simulation_date = fresh.simulation_date
        state.current_simulation_time = fresh.current_simulation_time
        state.day_start_at = fresh.day_start_at
        state.day_end_at = fresh.day_end_at
        state.speed_multiplier = fresh.speed_multiplier
        state.last_tick_real_time = None
        state.activity_log = fresh.activity_log
    session.flush()

    return {
        "scenario": _scenario_to_dict(scenario),
        "tickets": asdict(normal_result),
        "simulation_tickets": asdict(simulation_result),
        "state": _state_to_dict(session, state),
    }


def _simulation_ticket_to_dict(ticket: SimulationTicket) -> dict[str, Any]:
    requirements = _requirement_codes(ticket)
    return {
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


def _simulation_ticket_options():
    return (
        joinedload(SimulationTicket.subject),
        joinedload(SimulationTicket.location),
        joinedload(SimulationTicket.ticket_requirements).joinedload(TicketRequirement.requirement),
    )


def _require_not_running(session: Session) -> SimulationState:
    state = get_or_create_state(session)
    if state.status == SimulationStatus.RUNNING:
        raise SimulationIsRunningError(
            "Simulator tickets can only be changed when the simulation is paused or stopped."
        )
    return state


def _normalize_urgency(value: str | TicketUrgency) -> TicketUrgency:
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


def _parse_inject_at(state: SimulationState, inject_at: str | None, inject_time: str | None) -> datetime:
    raw_value = (inject_at or inject_time or "").strip()
    if not raw_value:
        raise ValueError("inject_at or inject_time is required")

    try:
        if "T" in raw_value or " " in raw_value:
            value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if value.tzinfo is not None:
                value = value.replace(tzinfo=None)
            return value

        parsed_time = time.fromisoformat(raw_value)
        return datetime.combine(state.simulation_date, parsed_time)
    except ValueError as exc:
        raise ValueError("inject_at must be an ISO datetime or inject_time must be HH:MM") from exc


def _validate_future_injection_time(state: SimulationState, inject_at: datetime) -> None:
    if inject_at <= state.current_simulation_time:
        raise ValueError(
            "inject_at must be later than the current simulation time "
            f"({state.current_simulation_time.strftime('%H:%M')})"
        )


def _requirement_codes_from_payload(payload: dict[str, Any]) -> list[str]:
    codes = {str(code).upper() for code in payload.get("requirements") or [] if str(code).strip()}
    if payload.get("requires_ladder"):
        codes.add("LADDER")
    if payload.get("requires_spring"):
        codes.add("VEER")
    return sorted(codes)


def _branch_for_state(session: Session, state: SimulationState) -> Branch:
    scenario = get_scenario_or_raise(state.scenario_id)
    return get_branch_by_name(session, scenario.branch_name)


_MANUAL_ADDRESS_PATTERN = re.compile(
    r"^\s*(?P<street>.+?)\s+"
    r"(?P<house_number>\d+[A-Za-z]?(?:[-/][0-9A-Za-z]+)?)\s*,\s*"
    r"(?P<city>[^,]+)"
    r"(?:\s*,\s*(?P<country>[^,]+))?\s*$"
)


def _parse_manual_address(address: str) -> dict[str, str]:
    address = str(address or "").strip()
    if not address:
        raise ValueError("Vul een adres in als: straat huisnummer, plaats")

    match = _MANUAL_ADDRESS_PATTERN.match(address)
    if match is None:
        raise ValueError("Adres moet het formaat 'straat huisnummer, plaats' hebben, bijvoorbeeld 'Kerkstraat 12, Den Bosch'.")

    raw = match.groupdict()
    parsed = {key: (value or "").strip() for key, value in raw.items()}
    parsed["country"] = parsed.get("country") or "Nederland"
    if not parsed["street"] or not parsed["house_number"] or not parsed["city"]:
        raise ValueError("Adres moet een straat, huisnummer en plaats bevatten.")
    return parsed


def validate_manual_address(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and geocode a manually entered simulator address.

    The simulator page only works with future rows in ``simulation_tickets``.
    This helper validates the free-text address before such a row is created or
    updated, so we never silently store a manual ticket on the branch coordinates.
    """
    address = str(payload.get("address") or "").strip()
    parsed = _parse_manual_address(address)

    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    if latitude is not None and longitude is not None:
        return {
            "address": address,
            "formatted_address": f'{parsed["street"]} {parsed["house_number"]}, {parsed["city"]}',
            "street": parsed["street"],
            "house_number": parsed["house_number"],
            "city": parsed["city"],
            "latitude": float(latitude),
            "longitude": float(longitude),
            "status": "resolved",
        }

    try:
        coordinates = coordinates_from_address(
            parsed["street"],
            parsed["house_number"],
            f'{parsed["city"]}, {parsed["country"]}',
        )
    except Exception as exc:  # pragma: no cover - depends on external geocoder availability
        raise ValueError("Adrescontrole is mislukt. Controleer je internetverbinding of probeer het later opnieuw.") from exc

    if coordinates.status != "resolved" or coordinates.latitude is None or coordinates.longitude is None:
        raise ValueError("Dit adres kon niet worden gevonden. Controleer straat, huisnummer en plaats.")

    return {
        "address": address,
        "formatted_address": f"{coordinates.street} {coordinates.house_number}, {coordinates.city}",
        "street": coordinates.street,
        "house_number": coordinates.house_number,
        "city": coordinates.city,
        "latitude": coordinates.latitude,
        "longitude": coordinates.longitude,
        "status": coordinates.status,
    }


def _get_or_create_manual_location(session: Session, *, branch: Branch, payload: dict[str, Any]) -> Location:
    location_id = payload.get("location_id")
    if location_id:
        location = session.get(Location, int(location_id))
        if location is None:
            raise ValueError(f"Location {location_id} was not found")
        return location

    resolved = validate_manual_address(payload)
    formatted_address = str(resolved["formatted_address"]).strip()

    existing = session.scalar(
        select(Location).where(
            (func.lower(Location.formatted_address) == formatted_address.lower())
            | (func.lower(Location.input_address) == formatted_address.lower())
        )
    )
    if existing is not None:
        return existing

    location = Location(
        input_address=formatted_address,
        formatted_address=formatted_address,
        street=resolved["street"],
        house_number=resolved["house_number"],
        city=resolved["city"],
        latitude=float(resolved["latitude"]),
        longitude=float(resolved["longitude"]),
    )
    session.add(location)
    session.flush()
    return location


def _replace_simulation_ticket_requirements(
    session: Session, *, simulation_ticket_id: int, requirement_codes: list[str]
) -> None:
    for link in session.scalars(
        select(TicketRequirement).where(TicketRequirement.simulation_ticket_id == simulation_ticket_id)
    ).all():
        session.delete(link)
    session.flush()
    add_requirement_links(
        session,
        requirement_codes=requirement_codes,
        simulation_ticket_id=simulation_ticket_id,
    )


def create_simulation_ticket(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_not_running(session)
    inject_at = _parse_inject_at(state, payload.get("inject_at"), payload.get("inject_time"))
    _validate_future_injection_time(state, inject_at)

    branch = _branch_for_state(session, state)
    location = _get_or_create_manual_location(session, branch=branch, payload=payload)
    subject_name = str(payload.get("subject") or "").strip()
    if not subject_name:
        raise ValueError("subject is required")
    subject = get_or_create_subject(session, subject_name)

    ticket = SimulationTicket(
        branch_id=branch.id,
        location_id=location.id,
        subject_id=subject.id,
        description=str(payload.get("description") or "Handmatig aangemaakt simulator ticket."),
        urgency=_normalize_urgency(payload.get("urgency", "medium")),
        created_at=inject_at,
    )
    session.add(ticket)
    session.flush()
    _replace_simulation_ticket_requirements(
        session,
        simulation_ticket_id=ticket.id,
        requirement_codes=_requirement_codes_from_payload(payload),
    )
    _append_log(state, f"Simulator ticket SIM-{ticket.id:03d} toegevoegd", actor="Planner", at=state.current_simulation_time)
    session.flush()

    ticket = session.scalars(
        select(SimulationTicket)
        .options(*_simulation_ticket_options())
        .where(SimulationTicket.id == ticket.id)
    ).unique().one()
    return _simulation_ticket_to_dict(ticket)


def update_simulation_ticket(session: Session, injection_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    state = _require_not_running(session)
    ticket = session.get(SimulationTicket, injection_id)
    if ticket is None:
        raise SimulationTicketNotFoundError(f"Simulation ticket {injection_id} was not found")

    inject_at = _parse_inject_at(
        state,
        payload.get("inject_at"),
        payload.get("inject_time") or ticket.created_at.strftime("%H:%M"),
    )
    _validate_future_injection_time(state, inject_at)

    branch = _branch_for_state(session, state)
    ticket.location_id = _get_or_create_manual_location(session, branch=branch, payload=payload).id

    subject_name = str(payload.get("subject") or "").strip()
    if not subject_name:
        raise ValueError("subject is required")
    ticket.subject_id = get_or_create_subject(session, subject_name).id
    ticket.description = str(payload.get("description") or ticket.description or "Handmatig aangepast simulator ticket.")
    ticket.urgency = _normalize_urgency(payload.get("urgency", ticket.urgency))
    ticket.created_at = inject_at

    _replace_simulation_ticket_requirements(
        session,
        simulation_ticket_id=ticket.id,
        requirement_codes=_requirement_codes_from_payload(payload),
    )
    _append_log(state, f"Simulator ticket SIM-{ticket.id:03d} aangepast", actor="Planner", at=state.current_simulation_time)
    session.flush()

    ticket = session.scalars(
        select(SimulationTicket)
        .options(*_simulation_ticket_options())
        .where(SimulationTicket.id == injection_id)
    ).unique().one()
    return _simulation_ticket_to_dict(ticket)


def list_planned_injections(session: Session) -> list[dict[str, Any]]:
    tickets = session.scalars(
        select(SimulationTicket)
        .options(*_simulation_ticket_options())
        .order_by(SimulationTicket.created_at.asc(), SimulationTicket.id.asc())
    ).unique().all()

    return [_simulation_ticket_to_dict(ticket) for ticket in tickets]


def _state_to_dict(session: Session, state: SimulationState) -> dict[str, Any]:
    scenario = get_scenario_or_raise(state.scenario_id)
    not_injected = session.scalar(select(func.count(SimulationTicket.id))) or 0
    activity_log = list(state.activity_log or [])
    injected_today = sum(1 for item in activity_log if "ingeschoten" in str(item.get("message", "")).lower())
    return {
        "scenario_id": scenario.scenario_id,
        "scenario": scenario.name,
        "current_time": state.current_simulation_time.strftime("%H:%M"),
        "current_simulation_time": state.current_simulation_time.isoformat(),
        "speed": state.speed_multiplier,
        "speed_multiplier": state.speed_multiplier,
        "status": STATUS_LABELS.get(state.status, state.status.value),
        "status_code": state.status.value,
        "worker_required": True,
        "stats": {
            "tickets_in_scenario": not_injected,
            "not_injected": not_injected,
            "injected_today": injected_today,
            "last_injection": activity_log[0].get("time", "–") if activity_log else "–",
        },
        "activity_log": activity_log,
    }


def get_state(session: Session) -> dict[str, Any]:
    state = get_or_create_state(session)
    return _state_to_dict(session, state)


def get_statistics(session: Session) -> dict[str, Any]:
    """Return a compact statistics payload for dashboards and polling clients."""
    state = get_or_create_state(session)
    state_dict = _state_to_dict(session, state)
    return {
        "scenario_id": state_dict["scenario_id"],
        "scenario": state_dict["scenario"],
        "status": state_dict["status"],
        "status_code": state_dict["status_code"],
        "current_time": state_dict["current_time"],
        "current_simulation_time": state_dict["current_simulation_time"],
        "speed": state_dict["speed"],
        "speed_multiplier": state_dict["speed_multiplier"],
        "stats": state_dict["stats"],
    }


def start(session: Session) -> dict[str, Any]:
    state = get_or_create_state(session)
    if state.status == SimulationStatus.COMPLETED:
        state.current_simulation_time = state.day_start_at
    state.status = SimulationStatus.RUNNING
    state.last_tick_real_time = datetime.now()
    _append_log(state, "Simulatie gestart", at=state.current_simulation_time)
    session.flush()
    return _state_to_dict(session, state)


def pause(session: Session) -> dict[str, Any]:
    state = get_or_create_state(session)
    advance_state_clock(state)
    state.status = SimulationStatus.PAUSED
    state.last_tick_real_time = None
    _append_log(state, "Simulatie gepauzeerd", at=state.current_simulation_time)
    session.flush()
    return _state_to_dict(session, state)


def stop(session: Session, clear_remaining_injections: bool = True) -> dict[str, Any]:
    state = get_or_create_state(session)
    state.status = SimulationStatus.STOPPED
    state.current_simulation_time = state.day_start_at
    state.last_tick_real_time = None
    if clear_remaining_injections:
        for ticket in session.scalars(select(SimulationTicket)).all():
            session.delete(ticket)
    _append_log(state, "Simulatie gestopt", at=state.current_simulation_time)
    session.flush()
    return _state_to_dict(session, state)


def set_speed(session: Session, speed_multiplier: int) -> dict[str, Any]:
    if speed_multiplier not in {1, 5, 10, 20, 50, 60, 100, 120}:
        raise ValueError("speed_multiplier must be one of: 1, 5, 10, 20, 50, 60, 100, 120")
    state = get_or_create_state(session)
    advance_state_clock(state)
    state.speed_multiplier = speed_multiplier
    if state.status == SimulationStatus.RUNNING:
        state.last_tick_real_time = datetime.now()
    _append_log(state, f"Snelheid gewijzigd naar {speed_multiplier}x", at=state.current_simulation_time)
    session.flush()
    return _state_to_dict(session, state)


def delete_injection(session: Session, injection_id: int) -> dict[str, Any]:
    state = _require_not_running(session)
    ticket = session.get(SimulationTicket, injection_id)
    if ticket is None:
        raise SimulationTicketNotFoundError(f"Simulation ticket {injection_id} was not found")
    session.delete(ticket)
    _append_log(state, f"Simulator ticket SIM-{injection_id:03d} verwijderd", actor="Planner", at=state.current_simulation_time)
    session.flush()
    return {"deleted": True, "id": injection_id}


def advance_state_clock(state: SimulationState, now: datetime | None = None) -> None:
    """Advance current_simulation_time from real elapsed time and speed."""
    if state.status != SimulationStatus.RUNNING:
        return
    now = now or datetime.now()
    if state.last_tick_real_time is None:
        state.last_tick_real_time = now
        return

    elapsed_real_seconds = max(0.0, (now - state.last_tick_real_time).total_seconds())
    elapsed_sim_seconds = elapsed_real_seconds * max(1, state.speed_multiplier)
    next_time = state.current_simulation_time + timedelta(seconds=elapsed_sim_seconds)

    if next_time >= state.day_end_at:
        state.current_simulation_time = state.day_end_at
        state.status = SimulationStatus.COMPLETED
        state.last_tick_real_time = None
        _append_log(state, "Simulatiedag afgerond", at=state.current_simulation_time)
    else:
        state.current_simulation_time = next_time
        state.last_tick_real_time = now


def inject_due_tickets(session: Session, state: SimulationState | None = None) -> int:
    state = state or get_or_create_state(session)
    current_dt = state.current_simulation_time

    due_tickets = session.scalars(
        select(SimulationTicket)
        .options(joinedload(SimulationTicket.ticket_requirements).joinedload(TicketRequirement.requirement))
        .where(SimulationTicket.created_at <= current_dt)
        .order_by(SimulationTicket.created_at.asc(), SimulationTicket.id.asc())
    ).unique().all()

    injected_count = 0
    for simulation_ticket in due_tickets:
        logger.info(
            "Transferring simulation ticket SIM-%03d into real tickets table at simulation time %s",
            simulation_ticket.id,
            current_dt.strftime("%H:%M:%S"),
        )
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
        _append_log(state, f"{injected_count} ticket(s) ingeschoten", at=current_dt)
        session.flush()
    return injected_count


def worker_tick(session: Session) -> dict[str, Any]:
    """One worker iteration: advance clock and inject due tickets."""
    state = get_or_create_state(session)
    if state.status != SimulationStatus.RUNNING:
        return {
            "status": state.status.value,
            "advanced": False,
            "current_simulation_time": state.current_simulation_time.isoformat(),
            "injected_count": 0,
        }

    advance_state_clock(state)
    injected_count = inject_due_tickets(session, state)
    session.flush()
    return {
        "status": state.status.value,
        "advanced": True,
        "current_simulation_time": state.current_simulation_time.isoformat(),
        "injected_count": injected_count,
    }
