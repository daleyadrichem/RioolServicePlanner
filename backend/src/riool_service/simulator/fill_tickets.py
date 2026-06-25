from __future__ import annotations

from datetime import date, timedelta

from riool_service.database.db_utils import session_scope
from riool_service.database.models.tickets import Ticket, TicketStatus

from riool_service.simulator.config import get_scenario
from riool_service.simulator.db_helpers import add_requirement_links, choose_location_near_branch, get_branch_by_name, get_or_create_subject
from riool_service.simulator.utils import clear_model_table, combine_day_and_time, make_rng, random_datetime_between
from riool_service.simulator.result import SeedResult
from riool_service.simulator.ticket_factory import generate_ticket_data


def seed_tickets(
    scenario_id: str = "normale_dag",
    *,
    simulation_date: date | None = None,
    count: int | None = None,
    seed: int | None = None
    ) -> SeedResult:
    """Fill the production ``tickets`` table directly.

    Parameters
    ----------
    scenario_id:
        Scenario id from ``ticket_scenarios_config.json``.
    simulation_date:
        Day used for generated ``created_at`` values. Defaults to today.
    count:
        Optional exact ticket count. When omitted, the scenario min/max is used.
    seed:
        Optional random seed for reproducible simulator output.
    config_path:
        Optional path to a scenario JSON file.
    """
    scenario = get_scenario(scenario_id)
    rng = make_rng(seed)
    day = (simulation_date or date.today()) - timedelta(days=1) # These filled tickets are for the previous day, so that they can be processed by the simulator on the current day.
    start_at = combine_day_and_time(day, scenario.day_start_time)
    end_at = combine_day_and_time(day, scenario.day_end_time)
    ticket_count = count or rng.randint(scenario.ticket_count_min, scenario.ticket_count_max)

    created_ids: list[int] = []
    skipped_requirement_links = 0
    notes: list[str] = []

    with session_scope() as session:
        clear_model_table(session, Ticket)
        branch = get_branch_by_name(session, scenario.branch_name)

        for _ in range(ticket_count):
            created_at = random_datetime_between(rng, start_at, end_at)
            generated = generate_ticket_data(
                rng=rng,
                scenario=scenario,
                created_at=created_at,
                include_urgent=False
            )
            subject = get_or_create_subject(session, generated.subject_name)
            location = choose_location_near_branch(
                session,
                rng,
                branch,
                radius_km=None,
            )

            ticket = Ticket(
                branch_id=branch.id,
                location_id=location.id,
                subject_id=subject.id,
                description=generated.description,
                urgency=generated.urgency,
                status=TicketStatus.OPEN,
                created_at=generated.created_at,
                deadline_at=generated.deadline_at,
            )
            session.add(ticket)
            session.flush()
            created_ids.append(ticket.id)

            added_links = add_requirement_links(
                session,
                requirement_codes=generated.requirement_codes,
                ticket_id=ticket.id,
            )
            skipped_requirement_links += max(0, len(generated.requirement_codes) - added_links)

    if skipped_requirement_links:
        notes.append(
            "Some requirement links were not inserted. Check whether the existing "
            "ticket_requirements table still has both parent foreign keys as NOT NULL."
        )

    return SeedResult(
        scenario_id=scenario.scenario_id,
        table_name="tickets",
        created_count=len(created_ids),
        created_ids=created_ids,
        skipped_requirement_links=skipped_requirement_links,
        notes=notes,
    )
