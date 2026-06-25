from __future__ import annotations

from datetime import date, timedelta

from riool_service.database.db_utils import session_scope
from riool_service.database.models.simulation_tickets import SimulationTicket

from riool_service.simulator.config import get_scenario
from riool_service.simulator.db_helpers import add_requirement_links, choose_location_near_branch, get_branch_by_name, get_or_create_subject
from riool_service.simulator.utils import clear_model_table, combine_day_and_time, make_rng, random_datetime_between
from riool_service.simulator.result import SeedResult
from riool_service.simulator.ticket_factory import generate_ticket_data


def _planned_submission_days(scenario) -> list[tuple[int, int]]:
    """Return ``[(day_offset, amount), ...]`` from scenario config."""
    schedule = scenario.simulation_ticket_submission_schedule
    if schedule:
        return [(int(item["dag_offset"]), int(item["aantal"])) for item in schedule]
    return [(0, scenario.simulation_ticket_count_min)]


def seed_simulation_tickets(
    scenario_id: str = "normale_dag",
    *,
    simulation_date: date | None = None,
    seed: int | None = None
) -> SeedResult:
    """Fill ``simulation_tickets`` with tickets planned throughout the day.

    For now ``SimulationTicket.created_at`` is used as the planned injection
    timestamp. Later, a simulator runner can fetch rows where
    ``created_at <= current_simulation_time`` and move them into ``tickets``.
    """
    scenario = get_scenario(scenario_id)
    rng = make_rng(seed)
    base_day = simulation_date or date.today()

    created_ids: list[int] = []
    skipped_requirement_links = 0
    notes: list[str] = []

    with session_scope() as session:
        clear_model_table(session, SimulationTicket)
        branch = get_branch_by_name(session, scenario.branch_name)

        for day_offset, amount in _planned_submission_days(scenario):
            current_day = base_day + timedelta(days=day_offset)
            start_at = combine_day_and_time(current_day, scenario.day_start_time)
            end_at = combine_day_and_time(current_day, scenario.day_end_time)

            for _ in range(amount):
                planned_submit_at = random_datetime_between(rng, start_at, end_at)
                generated = generate_ticket_data(
                    rng=rng,
                    scenario=scenario,
                    created_at=planned_submit_at,
                )
                subject = get_or_create_subject(session, generated.subject_name)
                location = choose_location_near_branch(
                    session,
                    rng,
                    branch,
                    radius_km=None,
                )

                simulation_ticket = SimulationTicket(
                    branch_id=branch.id,
                    location_id=location.id,
                    subject_id=subject.id,
                    description=generated.description,
                    urgency=generated.urgency,
                    created_at=planned_submit_at,
                )
                session.add(simulation_ticket)
                session.flush()
                created_ids.append(simulation_ticket.id)

                added_links = add_requirement_links(
                    session,
                    requirement_codes=generated.requirement_codes,
                    simulation_ticket_id=simulation_ticket.id,
                )
                skipped_requirement_links += max(0, len(generated.requirement_codes) - added_links)

    if skipped_requirement_links:
        notes.append(
            "Some requirement links were not inserted. Check whether the existing "
            "ticket_requirements table still has both parent foreign keys as NOT NULL."
        )

    return SeedResult(
        scenario_id=scenario.scenario_id,
        table_name="simulation_tickets",
        created_count=len(created_ids),
        created_ids=created_ids,
        skipped_requirement_links=skipped_requirement_links,
        notes=notes,
    )
