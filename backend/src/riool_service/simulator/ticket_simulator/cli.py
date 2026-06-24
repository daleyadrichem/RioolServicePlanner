"""Command-line interface for the ticket simulator."""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from riool_service.database.db_utils import session_scope

from . import model_imports as _model_imports  # noqa: F401
from .cleanup import clear_existing_tickets
from .config import (
    DEFAULT_SCENARIOS_CONFIG_PATH,
    SCENARIOS_CONFIG_ENV_VAR,
    load_scenarios,
    parse_created_date,
)
from .simulator import TicketSimulator


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(description="Generate simulated tickets.")
    parser.add_argument("scenario_id", help="Scenario ID from the scenarios JSON file.")
    parser.add_argument(
        "--scenarios",
        default=None,
        help=(
            f"Path to scenarios JSON file. Defaults to "
            f"${SCENARIOS_CONFIG_ENV_VAR} or {DEFAULT_SCENARIOS_CONFIG_PATH}."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of tickets to generate. Defaults to scenario min/max.",
    )
    parser.add_argument(
        "--created-date",
        default=None,
        help=(
            "Base date for generated tickets, for example "
            "2026-06-23 or 2026-06-23T09:00:00+00:00"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible test data.",
    )
    parser.add_argument(
        "--clear-existing-tickets",
        action="store_true",
        help=(
            "Delete existing tickets before generating new ones. "
            "Does not touch technician or planner tables."
        ),
    )
    return parser


def main() -> None:
    """Run the ticket simulator CLI."""
    args = build_parser().parse_args()
    load_dotenv()

    scenarios_path = args.scenarios or os.getenv(
        SCENARIOS_CONFIG_ENV_VAR,
        DEFAULT_SCENARIOS_CONFIG_PATH,
    )
    scenarios = load_scenarios(scenarios_path)
    created_date = parse_created_date(args.created_date)

    with session_scope() as session:
        if args.clear_existing_tickets:
            clear_existing_tickets(session)

        simulator = TicketSimulator(
            session=session, scenarios=scenarios, seed=args.seed
        )
        tickets = simulator.generate_random_tickets(
            scenario_id=args.scenario_id,
            ticket_count=args.count,
            created_date=created_date,
        )

        print(f"Generated {len(tickets)} tickets for scenario {args.scenario_id!r}")
        for ticket in tickets:
            urgency = (
                ticket.urgency.value
                if hasattr(ticket.urgency, "value")
                else ticket.urgency
            )
            print(f"- #{ticket.id}: {ticket.subject.name} ({urgency})")


if __name__ == "__main__":
    main()
