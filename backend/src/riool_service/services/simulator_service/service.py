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
from riool_service.database.models.simulated_technician import SimulatedTechnicianState
from riool_service.database.models.technician import Technician
from riool_service.database.models.simulation_tickets import SimulationTicket
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.database.models.planning_assignment import PlanningAssignment, PlanningAssignmentStatus
from riool_service.database.models.planning_run import PlanningRun, PlanningRunStatus
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.db_utils import get_engine
from riool_service.database_initializer.database import create_schema
from riool_service.simulator.config import ScenarioConfig, TICKET_SCENARIOS_CONFIG_ENV_VAR, load_scenarios
from riool_service.simulator.fill_simulation_tickets import seed_simulation_tickets
from riool_service.simulator.fill_tickets import seed_tickets
from riool_service.simulator.db_helpers import add_requirement_links, get_branch_by_name, get_or_create_subject
from riool_service.simulator.utils import clear_rows_by_description_marker, deadline_for, parse_time
from riool_service.geocode_service import coordinates_from_address

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "ticket_scenarios_config.json"
DEFAULT_STATE_ID = 1
DEFAULT_SPEED = 1
ALLOWED_SPEEDS = {1, 10, 60, 120}
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
    """Create simulator tables/columns that may not exist yet in older local databases."""
    create_schema(get_engine())


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
        if state.speed_multiplier not in ALLOWED_SPEEDS:
            state.speed_multiplier = DEFAULT_SPEED
            session.flush()
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
    effective_seed = seed if seed is not None else scenario.seed

    used_address_keys: set[str] = set()

    normal_result = seed_tickets(
        scenario.scenario_id,
        simulation_date=simulation_day,
        seed=effective_seed,
        used_address_keys=used_address_keys,
    )

    simulation_result = seed_simulation_tickets(
        scenario.scenario_id,
        simulation_date=simulation_day,
        seed=None if effective_seed is None else effective_seed + 1,
        used_address_keys=used_address_keys,
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
        "requires_supplies": "SUPPLIES" in requirements,
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
    if payload.get("requires_supplies"):
        codes.add("SUPPLIES")
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

    deleted_simulation_tickets = 0
    deleted_real_tickets = 0
    if clear_remaining_injections:
        # Keep manually created tickets safe. Only remove tickets that were
        # generated by the simulator scenario, both from the future injection
        # queue and from the real tickets table after they have been injected.
        marker = "Simulator ticket voor scenario"

        deleted_simulation_tickets += clear_rows_by_description_marker(session, SimulationTicket, marker)
        deleted_real_tickets += clear_rows_by_description_marker(session, Ticket, marker)

    if deleted_simulation_tickets or deleted_real_tickets:
        _append_log(
            state,
            f"Simulator tickets opgeschoond: {deleted_simulation_tickets} wachtrij, {deleted_real_tickets} tickets",
            at=state.current_simulation_time,
        )
    _append_log(state, "Simulatie gestopt", at=state.current_simulation_time)
    session.flush()
    return _state_to_dict(session, state)


def set_speed(session: Session, speed_multiplier: int) -> dict[str, Any]:
    if speed_multiplier not in ALLOWED_SPEEDS:
        raise ValueError("speed_multiplier must be one of: 1, 10, 60, 120")
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
        session.flush()
        injected_count += 1

    if injected_count:
        # Incremental planning is handled by the separate planning worker, not
        # by the simulator injection worker itself.
        _append_log(state, f"{injected_count} ticket(s) ingeschoten", at=current_dt)
        session.flush()
    return injected_count


def worker_tick(session: Session) -> dict[str, Any]:
    """One worker iteration: advance clock, inject due tickets, and update assignment progress."""
    state = get_or_create_state(session)
    if state.status != SimulationStatus.RUNNING:
        return {
            "status": state.status.value,
            "advanced": False,
            "current_simulation_time": state.current_simulation_time.isoformat(),
            "injected_count": 0,
            "assignment_status_updates": 0,
        }

    advance_state_clock(state)
    injected_count = inject_due_tickets(session, state)
    assignment_status_updates = update_planning_assignment_statuses_for_simulation(session, state)
    session.flush()
    return {
        "status": state.status.value,
        "advanced": True,
        "current_simulation_time": state.current_simulation_time.isoformat(),
        "injected_count": injected_count,
        "assignment_status_updates": assignment_status_updates,
    }


# --------------------------
# Technician progress simulator
# --------------------------

_VISIBLE_SIM_ASSIGNMENT_STATUSES = {
    PlanningAssignmentStatus.PLANNED,
    PlanningAssignmentStatus.DRIVING,
    PlanningAssignmentStatus.IN_PROGRESS,
    PlanningAssignmentStatus.COMPLETED,
}

_SIMULATED_TIME_SCOPE_DRIVING = "driving_to_ticket"
_SIMULATED_TIME_SCOPE_TICKET = "ticket"
_SIMULATED_TIME_SCOPES = {_SIMULATED_TIME_SCOPE_DRIVING, _SIMULATED_TIME_SCOPE_TICKET}


def _latest_completed_planning_run_for_branch(session: Session, branch_id: int) -> PlanningRun | None:
    return session.scalar(
        select(PlanningRun)
        .where(PlanningRun.branch_id == branch_id, PlanningRun.status == PlanningRunStatus.COMPLETED)
        .order_by(PlanningRun.completed_at.desc().nullslast(), PlanningRun.id.desc())
        .limit(1)
    )


def _parse_hhmm_on_simulation_day(state: SimulationState, value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = time.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return datetime.combine(state.current_simulation_time.date(), parsed)


def _time_range_contains(state: SimulationState, start_hhmm: str | None, end_hhmm: str | None) -> bool:
    start_at = _parse_hhmm_on_simulation_day(state, start_hhmm)
    end_at = _parse_hhmm_on_simulation_day(state, end_hhmm)
    if start_at is None or end_at is None:
        return False
    return start_at <= state.current_simulation_time < end_at


def _minutes_until(state: SimulationState, end_hhmm: str | None) -> int | None:
    end_at = _parse_hhmm_on_simulation_day(state, end_hhmm)
    if end_at is None:
        return None
    return max(0, int(round((end_at - state.current_simulation_time).total_seconds() / 60)))


def _assignment_status_for_item_type(item_type: str | None) -> PlanningAssignmentStatus | None:
    if item_type == "travel":
        return PlanningAssignmentStatus.DRIVING
    if item_type == "ticket":
        return PlanningAssignmentStatus.IN_PROGRESS
    if item_type == "completed":
        return PlanningAssignmentStatus.COMPLETED
    return None


def _simulation_datetimes_for_assignment(
    state: SimulationState,
    assignment: PlanningAssignment,
) -> tuple[datetime, datetime, datetime]:
    """Return travel_start, planned_start, planned_end on the active simulation date."""
    current_time = state.current_simulation_time
    planned_start = datetime.combine(current_time.date(), assignment.planned_start_at.time())
    planned_end = datetime.combine(current_time.date(), assignment.planned_end_at.time())
    if planned_end <= planned_start:
        planned_end += timedelta(days=1)
    travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
    travel_start = planned_start - timedelta(minutes=travel_minutes)
    return travel_start, planned_start, planned_end


def _current_phase_for_assignment(
    state: SimulationState,
    assignment: PlanningAssignment,
) -> tuple[str | None, datetime | None, datetime | None, PlanningAssignmentStatus]:
    """Return phase, phase_start, phase_end and status for one assignment."""
    current_time = state.current_simulation_time
    travel_start, planned_start, planned_end = _simulation_datetimes_for_assignment(state, assignment)

    if travel_start <= current_time < planned_start and int(assignment.estimated_travel_minutes_before or 0) > 0:
        return _SIMULATED_TIME_SCOPE_DRIVING, travel_start, planned_start, PlanningAssignmentStatus.DRIVING
    if planned_start <= current_time < planned_end:
        return _SIMULATED_TIME_SCOPE_TICKET, planned_start, planned_end, PlanningAssignmentStatus.IN_PROGRESS
    if current_time >= planned_end:
        return None, None, None, PlanningAssignmentStatus.COMPLETED
    return None, None, None, PlanningAssignmentStatus.PLANNED


def _simulation_status_for_assignment(
    state: SimulationState,
    assignment: PlanningAssignment,
) -> PlanningAssignmentStatus:
    """Return the assignment status that matches the current simulation time."""
    return _current_phase_for_assignment(state, assignment)[3]




def _simulation_status_for_assignment_with_technician_state(
    state: SimulationState,
    assignment: PlanningAssignment,
    technician_state: SimulatedTechnicianState,
) -> PlanningAssignmentStatus:
    """Return status, respecting a delayed simulated phase as leading time."""
    if (
        technician_state.planning_assignment_id == assignment.id
        and technician_state.simulated_time_applies_to in _SIMULATED_TIME_SCOPES
        and technician_state.simulated_end_at is not None
    ):
        planned_phase_end = _planned_phase_end_for_scope(state, assignment, technician_state.simulated_time_applies_to)
        if (
            planned_phase_end is not None
            and technician_state.simulated_end_at > planned_phase_end
            and state.current_simulation_time < technician_state.simulated_end_at
        ):
            if technician_state.simulated_time_applies_to == _SIMULATED_TIME_SCOPE_DRIVING:
                return PlanningAssignmentStatus.DRIVING
            return PlanningAssignmentStatus.IN_PROGRESS
    return _simulation_status_for_assignment(state, assignment)

def _shift_assignment_schedule(assignment: PlanningAssignment, delta: timedelta, *, shift_start: bool, shift_end: bool) -> None:
    if shift_start:
        assignment.planned_start_at = assignment.planned_start_at + delta
    if shift_end:
        assignment.planned_end_at = assignment.planned_end_at + delta


def _shift_later_assignments(assignments: list[PlanningAssignment], current_assignment: PlanningAssignment, delta: timedelta) -> None:
    for later in assignments:
        if later.sequence_order > current_assignment.sequence_order:
            later.planned_start_at = later.planned_start_at + delta
            later.planned_end_at = later.planned_end_at + delta


def _planned_phase_end_for_scope(
    state: SimulationState,
    assignment: PlanningAssignment,
    scope: str | None,
) -> datetime | None:
    _travel_start, planned_start, planned_end = _simulation_datetimes_for_assignment(state, assignment)
    if scope == _SIMULATED_TIME_SCOPE_DRIVING:
        return planned_start
    if scope == _SIMULATED_TIME_SCOPE_TICKET:
        return planned_end
    return None


def _is_delayed_override_active(
    state: SimulationState,
    assignment: PlanningAssignment,
    technician_state: SimulatedTechnicianState,
) -> bool:
    """Return whether this assignment is being held by a delayed simulated end.

    A delayed override belongs to exactly one assignment and one phase. While it
    is active, that assignment is the technician's leading piece of work and all
    later assignments for the same technician must stay planned, even if their
    original planned time window has already arrived.
    """
    if (
        technician_state.planning_assignment_id != assignment.id
        or technician_state.simulated_time_applies_to not in _SIMULATED_TIME_SCOPES
        or technician_state.simulated_end_at is None
    ):
        return False

    planned_phase_end = _planned_phase_end_for_scope(state, assignment, technician_state.simulated_time_applies_to)
    return (
        planned_phase_end is not None
        and technician_state.simulated_end_at > planned_phase_end
        and state.current_simulation_time < technician_state.simulated_end_at
    )


def _delayed_override_anchor(
    state: SimulationState,
    assignments: list[PlanningAssignment],
    technician_state: SimulatedTechnicianState,
) -> PlanningAssignment | None:
    by_id = {assignment.id: assignment for assignment in assignments}
    assignment = by_id.get(technician_state.planning_assignment_id or 0)
    if assignment is not None and _is_delayed_override_active(state, assignment, technician_state):
        return assignment
    return None


def _ticket_status_for_assignment_status(
    assignment_status: PlanningAssignmentStatus,
    *,
    delayed: bool,
) -> TicketStatus:
    if delayed:
        return TicketStatus.DELAYED
    if assignment_status == PlanningAssignmentStatus.IN_PROGRESS:
        return TicketStatus.IN_PROGRESS
    if assignment_status == PlanningAssignmentStatus.COMPLETED:
        return TicketStatus.COMPLETED
    return TicketStatus.PLANNED


def _sync_ticket_status_for_assignment(
    assignment: PlanningAssignment,
    assignment_status: PlanningAssignmentStatus,
    *,
    delayed: bool = False,
) -> int:
    ticket = assignment.ticket
    if ticket is None or ticket.status == TicketStatus.CANCELLED:
        return 0

    next_ticket_status = _ticket_status_for_assignment_status(assignment_status, delayed=delayed)
    if ticket.status == next_ticket_status:
        return 0
    ticket.status = next_ticket_status
    if next_ticket_status == TicketStatus.IN_PROGRESS and ticket.started_at is None:
        ticket.started_at = assignment.planned_start_at
    if next_ticket_status == TicketStatus.COMPLETED and ticket.completed_at is None:
        ticket.completed_at = assignment.planned_end_at
    if next_ticket_status != TicketStatus.COMPLETED:
        ticket.completed_at = None
    return 1


def _apply_simulated_phase_end_for_technician(
    *,
    state: SimulationState,
    technician_state: SimulatedTechnicianState,
    assignments: list[PlanningAssignment],
) -> int:
    """Apply the frontend-provided simulated end time for the current drive/work phase.

    The simulated end time is leading: a delayed phase must not progress at its
    planned end, and an early phase may progress as soon as the simulated end is
    reached. When that simulated boundary is reached, the current assignment and
    all later assignments for the technician are shifted by the delta.
    """
    if not assignments:
        if technician_state.planning_assignment_id is not None or technician_state.simulated_time_applies_to is not None:
            technician_state.planning_assignment_id = None
            technician_state.simulated_time_applies_to = None
            technician_state.simulated_end_at = None
            return 1
        return 0

    current_time = state.current_simulation_time
    ordered = sorted(assignments, key=lambda row: (row.sequence_order, row.planned_start_at, row.id))
    by_id = {row.id: row for row in ordered}
    changed = 0

    override_assignment = by_id.get(technician_state.planning_assignment_id or 0)
    override_scope = technician_state.simulated_time_applies_to
    simulated_end = technician_state.simulated_end_at

    active_assignment: PlanningAssignment | None = None
    active_scope: str | None = None
    planned_phase_end: datetime | None = None

    if override_assignment is not None and override_scope in _SIMULATED_TIME_SCOPES and simulated_end is not None:
        override_planned_end = _planned_phase_end_for_scope(state, override_assignment, override_scope)
        if override_planned_end is not None:
            # The override belongs to the phase that was active when the user
            # edited the simulated end time. Apply the resulting schedule delta
            # at that exact simulated boundary before falling back to normal
            # time-based detection. Without this, a delayed ticket that reached
            # its simulated end could be skipped as already completed, causing
            # the next ticket to be evaluated against its old timestamps.
            if current_time >= simulated_end:
                delta = simulated_end - override_planned_end
                if delta != timedelta(0):
                    if override_scope == _SIMULATED_TIME_SCOPE_DRIVING:
                        # Driving ends at planned_start_at. Moving it also moves
                        # the ticket work window and every later assignment.
                        _shift_assignment_schedule(override_assignment, delta, shift_start=True, shift_end=True)
                        _shift_later_assignments(ordered, override_assignment, delta)
                    else:
                        # Ticket work ends at planned_end_at. Its start stays
                        # fixed, but its end and every later assignment move.
                        _shift_assignment_schedule(override_assignment, delta, shift_start=False, shift_end=True)
                        _shift_later_assignments(ordered, override_assignment, delta)
                    changed += 1

                technician_state.simulated_end_at = None
                simulated_end = None
                changed += 1
            # If the override is a delay, keep this phase active past the planned
            # boundary until the simulated end is reached. This prevents the normal
            # time-based status calculation from completing it too early.
            elif simulated_end > override_planned_end:
                active_assignment = override_assignment
                active_scope = override_scope
                planned_phase_end = override_planned_end

    if active_assignment is None:
        for assignment in ordered:
            phase, _phase_start, phase_end, status = _current_phase_for_assignment(state, assignment)
            if status in {PlanningAssignmentStatus.DRIVING, PlanningAssignmentStatus.IN_PROGRESS}:
                active_assignment = assignment
                active_scope = phase
                planned_phase_end = phase_end
                break
            if status == PlanningAssignmentStatus.PLANNED:
                break

    if active_assignment is None or active_scope is None or planned_phase_end is None:
        # No active drive/job right now. Keep the row pointed at the next assignment
        # for the UI, but clear any old override so it cannot affect the wrong phase.
        next_assignment = next((row for row in ordered if row.status != PlanningAssignmentStatus.COMPLETED), None)
        next_id = next_assignment.id if next_assignment is not None else None
        if technician_state.planning_assignment_id != next_id:
            technician_state.planning_assignment_id = next_id
            changed += 1
        if technician_state.simulated_time_applies_to is not None:
            technician_state.simulated_time_applies_to = None
            changed += 1
        if technician_state.simulated_end_at is not None:
            technician_state.simulated_end_at = None
            changed += 1
        return changed

    if technician_state.planning_assignment_id != active_assignment.id:
        technician_state.planning_assignment_id = active_assignment.id
        technician_state.simulated_end_at = None
        simulated_end = None
        changed += 1
    if technician_state.simulated_time_applies_to != active_scope:
        technician_state.simulated_time_applies_to = active_scope
        technician_state.simulated_end_at = None
        simulated_end = None
        changed += 1

    if simulated_end is None:
        return changed

    # Only move the plan at the simulated boundary. For delays this means the
    # assignment stays in the active phase until simulated_end, not planned_end.
    if current_time < simulated_end:
        return changed

    delta = simulated_end - planned_phase_end
    if delta == timedelta(0):
        technician_state.simulated_end_at = None
        return changed + 1

    if active_scope == _SIMULATED_TIME_SCOPE_DRIVING:
        # Driving ends at planned_start_at. Moving it also moves the ticket work
        # window and every later assignment for this technician.
        _shift_assignment_schedule(active_assignment, delta, shift_start=True, shift_end=True)
        _shift_later_assignments(ordered, active_assignment, delta)
    else:
        # Ticket work ends at planned_end_at. The ticket start stays fixed, but
        # its end and all later assignments move by the simulated delta.
        _shift_assignment_schedule(active_assignment, delta, shift_start=False, shift_end=True)
        _shift_later_assignments(ordered, active_assignment, delta)

    technician_state.simulated_end_at = None
    return changed + 1


def update_planning_assignment_statuses_for_simulation(
    session: Session,
    state: SimulationState | None = None,
    branch_id: int | None = None,
) -> int:
    """Persist simulated schedule shifts and assignment status transitions from the worker tick."""
    state = state or get_or_create_state(session)
    branch = _branch_for_state(session, state) if branch_id is None else session.get(Branch, int(branch_id))
    if branch is None:
        return 0

    latest_run = _latest_completed_planning_run_for_branch(session, branch.id)
    if latest_run is None:
        return 0

    assignments = list(
        session.scalars(
            select(PlanningAssignment)
            .where(
                PlanningAssignment.planning_run_id == latest_run.id,
                PlanningAssignment.status.notin_([PlanningAssignmentStatus.CANCELLED, PlanningAssignmentStatus.MOVED]),
            )
            .order_by(PlanningAssignment.technician_id, PlanningAssignment.sequence_order, PlanningAssignment.id)
        ).all()
    )

    by_technician: dict[int, list[PlanningAssignment]] = {}
    for assignment in assignments:
        by_technician.setdefault(assignment.technician_id, []).append(assignment)

    updated_count = 0
    for technician_id, technician_assignments in by_technician.items():
        technician_state = _get_or_create_technician_state(session, technician_id)
        updated_count += _apply_simulated_phase_end_for_technician(
            state=state,
            technician_state=technician_state,
            assignments=technician_assignments,
        )

        delayed_anchor = _delayed_override_anchor(state, technician_assignments, technician_state)

        for assignment in technician_assignments:
            assignment_is_delayed_anchor = delayed_anchor is not None and assignment.id == delayed_anchor.id
            assignment_is_blocked_later_work = (
                delayed_anchor is not None
                and assignment.sequence_order > delayed_anchor.sequence_order
            )

            if assignment_is_blocked_later_work:
                next_status = PlanningAssignmentStatus.PLANNED
            else:
                next_status = _simulation_status_for_assignment_with_technician_state(state, assignment, technician_state)

            if assignment.status != next_status:
                assignment.status = next_status
                updated_count += 1
            updated_count += _sync_ticket_status_for_assignment(
                assignment,
                next_status,
                delayed=assignment_is_delayed_anchor,
            )

    if updated_count:
        session.flush()
    return updated_count

def _assignment_ticket_dict(
    assignment: PlanningAssignment | None,
    current_status: PlanningAssignmentStatus | None = None,
) -> dict[str, Any] | None:
    if assignment is None or assignment.ticket is None:
        return None
    ticket = assignment.ticket
    status = current_status or assignment.status
    return {
        "assignment_id": assignment.id,
        "ticket_id": ticket.id,
        "ticket_display_id": f"T-{ticket.id:03d}",
        "subject": ticket.subject.name if ticket.subject else "Onbekend",
        "address": _ticket_address_from_real_ticket(ticket),
        "urgency": _value(ticket.urgency),
        "assignment_status": _value(status),
    }


def _ticket_address_from_real_ticket(ticket: Ticket) -> str:
    location = ticket.location
    if location is None:
        return ""
    if location.street and location.house_number and location.city:
        return f"{location.street} {location.house_number}, {location.city}"
    value = location.formatted_address or location.input_address or location.city or ""
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return ", ".join(parts[:2]) if len(parts) > 2 else str(value).strip()


def _mechanic_status_label(item_type: str | None, has_future_work: bool) -> str:
    if item_type == "ticket":
        return "Werkt aan ticket"
    if item_type == "travel":
        return "Onderweg"
    if item_type == "requirement_pickup":
        return "Hulpmiddelen ophalen"
    if item_type == "break":
        return "Pauze"
    return "Wacht op volgende taak" if has_future_work else "Geen geplande taak"


def _mechanic_status_code(item_type: str | None, has_future_work: bool) -> str:
    if item_type in {"ticket", "travel", "requirement_pickup", "break"}:
        return item_type
    return "waiting" if has_future_work else "idle"


def _default_simulated_time_scope(item_type: str | None) -> str | None:
    if item_type == "travel":
        return _SIMULATED_TIME_SCOPE_DRIVING
    if item_type == "ticket":
        return _SIMULATED_TIME_SCOPE_TICKET
    return None


def _normalize_simulated_time_scope(value: Any) -> str | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "drive": _SIMULATED_TIME_SCOPE_DRIVING,
        "driving": _SIMULATED_TIME_SCOPE_DRIVING,
        "travel": _SIMULATED_TIME_SCOPE_DRIVING,
        "driving_to_ticket": _SIMULATED_TIME_SCOPE_DRIVING,
        "drive_to_ticket": _SIMULATED_TIME_SCOPE_DRIVING,
        "towards_ticket": _SIMULATED_TIME_SCOPE_DRIVING,
        "ticket": _SIMULATED_TIME_SCOPE_TICKET,
        "work": _SIMULATED_TIME_SCOPE_TICKET,
        "ticket_work": _SIMULATED_TIME_SCOPE_TICKET,
        "ticket_itself": _SIMULATED_TIME_SCOPE_TICKET,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _SIMULATED_TIME_SCOPES:
        raise ValueError("simulated_time_applies_to must be driving_to_ticket or ticket")
    return normalized


def _current_assignment_for_technician(
    session: Session,
    *,
    state: SimulationState,
    technician: Technician,
    assignments: list[PlanningAssignment],
) -> tuple[PlanningAssignment | None, str | None, str | None, str | None, bool]:
    """Return current assignment, item type, planned start, planned end, has_future_work."""
    sorted_assignments = sorted(assignments, key=lambda row: (row.planned_start_at, row.sequence_order, row.id))
    current_time = state.current_simulation_time
    has_future_work = any(row.planned_start_at and row.planned_start_at > current_time for row in sorted_assignments)
    last_completed: tuple[PlanningAssignment, str, str] | None = None

    for assignment in sorted_assignments:
        # Compare by time-of-day on the active simulation date. This keeps the
        # mechanic simulator useful even when a local demo database contains a
        # plan generated on a different date than today's simulator clock.
        planned_start = datetime.combine(current_time.date(), assignment.planned_start_at.time())
        planned_end = datetime.combine(current_time.date(), assignment.planned_end_at.time())
        if planned_end <= planned_start:
            planned_end += timedelta(days=1)
        travel_minutes = int(assignment.estimated_travel_minutes_before or 0)
        travel_start = planned_start - timedelta(minutes=travel_minutes)
        if travel_minutes > 0 and travel_start <= current_time < planned_start:
            return assignment, "travel", travel_start.strftime("%H:%M"), planned_start.strftime("%H:%M"), True
        if planned_start <= current_time < planned_end:
            return assignment, "ticket", planned_start.strftime("%H:%M"), planned_end.strftime("%H:%M"), True
        if assignment.status == PlanningAssignmentStatus.COMPLETED and current_time >= planned_end:
            last_completed = (assignment, planned_start.strftime("%H:%M"), planned_end.strftime("%H:%M"))

    if last_completed is not None:
        assignment, planned_start, planned_end = last_completed
        return assignment, "completed", planned_start, planned_end, has_future_work

    return None, None, None, None, has_future_work


def _get_or_create_technician_state(session: Session, technician_id: int) -> SimulatedTechnicianState:
    row = session.scalar(
        select(SimulatedTechnicianState).where(SimulatedTechnicianState.technician_id == technician_id).limit(1)
    )
    if row is not None:
        return row
    row = SimulatedTechnicianState(technician_id=technician_id)
    session.add(row)
    session.flush()
    return row


def list_technician_simulation_states(session: Session, branch_id: int | None = None) -> list[dict[str, Any]]:
    """Return the current simulator progress card for every active mechanic."""
    state = get_or_create_state(session)
    branch = _branch_for_state(session, state) if branch_id is None else session.get(Branch, int(branch_id))
    if branch is None:
        return []

    technicians = list(
        session.scalars(
            select(Technician)
            .where(Technician.branch_id == branch.id)
            .order_by(Technician.name.asc(), Technician.id.asc())
        ).all()
    )
    latest_run = _latest_completed_planning_run_for_branch(session, branch.id)
    assignments: list[PlanningAssignment] = []
    if latest_run is not None:
        assignments = list(
            session.scalars(
                select(PlanningAssignment)
                .options(
                    joinedload(PlanningAssignment.ticket).joinedload(Ticket.subject),
                    joinedload(PlanningAssignment.ticket).joinedload(Ticket.location),
                )
                .where(
                    PlanningAssignment.planning_run_id == latest_run.id,
                    PlanningAssignment.status.in_(list(_VISIBLE_SIM_ASSIGNMENT_STATUSES)),
                )
                .order_by(PlanningAssignment.technician_id, PlanningAssignment.sequence_order, PlanningAssignment.id)
            ).unique().all()
        )

    by_technician: dict[int, list[PlanningAssignment]] = {}
    for assignment in assignments:
        by_technician.setdefault(assignment.technician_id, []).append(assignment)

    rows: list[dict[str, Any]] = []
    for technician in technicians:
        technician_state = _get_or_create_technician_state(session, technician.id)
        assignment, item_type, planned_start, planned_end, has_future_work = _current_assignment_for_technician(
            session,
            state=state,
            technician=technician,
            assignments=by_technician.get(technician.id, []),
        )
        delayed_anchor = _delayed_override_anchor(state, by_technician.get(technician.id, []), technician_state)
        if delayed_anchor is not None:
            travel_start, planned_start_at, planned_end_at = _simulation_datetimes_for_assignment(state, delayed_anchor)
            assignment = delayed_anchor
            if technician_state.simulated_time_applies_to == _SIMULATED_TIME_SCOPE_DRIVING:
                item_type = "travel"
                planned_start = travel_start.strftime("%H:%M")
                planned_end = planned_start_at.strftime("%H:%M")
            else:
                item_type = "ticket"
                planned_start = planned_start_at.strftime("%H:%M")
                planned_end = planned_end_at.strftime("%H:%M")
            has_future_work = True
        assignment_id = assignment.id if assignment is not None else None
        default_time_scope = _default_simulated_time_scope(item_type)
        current_assignment_status = _assignment_status_for_item_type(item_type)
        # if technician_state.planning_assignment_id != assignment_id:
        #     technician_state.planning_assignment_id = assignment_id
        # technician_state.simulated_time_applies_to = default_time_scope
        planned_minutes_remaining = _minutes_until(state, planned_end)
        simulated_end_time = _format_time(technician_state.simulated_end_at)
        simulated_minutes_remaining = None
        if technician_state.simulated_end_at is not None:
            simulated_minutes_remaining = max(
                0,
                int(round((technician_state.simulated_end_at - state.current_simulation_time).total_seconds() / 60)),
            )
        rows.append(
            {
                "id": technician_state.id,
                "technician": {
                    "id": technician.id,
                    "name": technician.name,
                    "branch_id": technician.branch_id,
                },
                "status": _mechanic_status_label(item_type, has_future_work),
                "status_code": _mechanic_status_code(item_type, has_future_work),
                "planning_assignment_id": assignment_id,
                "current_ticket": _assignment_ticket_dict(assignment, current_assignment_status),
                "planned_start_time": planned_start,
                "planned_end_time": planned_end,
                "planned_minutes_remaining": planned_minutes_remaining,
                "simulated_end_time": simulated_end_time,
                "simulated_end_at": technician_state.simulated_end_at.isoformat() if technician_state.simulated_end_at else None,
                "simulated_time_applies_to": technician_state.simulated_time_applies_to,
                "simulated_minutes_remaining": simulated_minutes_remaining,
                "effective_end_time": simulated_end_time or planned_end,
                "effective_minutes_remaining": simulated_minutes_remaining if simulated_minutes_remaining is not None else planned_minutes_remaining,
            }
        )
    session.flush()
    return rows


def update_technician_simulation_state(session: Session, technician_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    state = get_or_create_state(session)
    technician = session.get(Technician, int(technician_id))
    if technician is None:
        raise ValueError(f"Technician {technician_id} was not found")
    row = _get_or_create_technician_state(session, technician.id)

    assignment_id = payload.get("planning_assignment_id")
    if assignment_id is not None:
        assignment = session.get(PlanningAssignment, int(assignment_id))
        if assignment is None or assignment.technician_id != technician.id:
            raise ValueError("planning_assignment_id does not belong to this technician")
        row.planning_assignment_id = assignment.id

    raw_scope = (
        payload.get("simulated_time_applies_to")
        if "simulated_time_applies_to" in payload
        else payload.get("time_applies_to")
        if "time_applies_to" in payload
        else payload.get("applies_to")
    )
    if raw_scope is not None:
        row.simulated_time_applies_to = _normalize_simulated_time_scope(raw_scope)

    raw_end = payload.get("simulated_end_at") or payload.get("end_at") or payload.get("simulated_end_time") or payload.get("end_time")
    if raw_end in {None, ""}:
        row.simulated_end_at = None
    else:
        raw_text = str(raw_end).strip()
        try:
            if "T" in raw_text or " " in raw_text:
                parsed = datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.replace(tzinfo=None)
            else:
                parsed_time = time.fromisoformat(raw_text)
                parsed = datetime.combine(state.current_simulation_time.date(), parsed_time)
        except ValueError as exc:
            raise ValueError("end_time must be HH:MM or an ISO datetime") from exc
        row.simulated_end_at = parsed

    update_planning_assignment_statuses_for_simulation(session, state, branch_id=technician.branch_id)
    session.flush()
    return next(
        item for item in list_technician_simulation_states(session, branch_id=technician.branch_id)
        if item["technician"]["id"] == technician.id
    )
