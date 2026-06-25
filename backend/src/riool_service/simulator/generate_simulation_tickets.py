from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from riool_service.simulator.fill_simulation_tickets import seed_simulation_tickets
from riool_service.simulator.fill_tickets import seed_tickets
from riool_service.simulator.result import SeedResult


def _parse_date(value: str) -> date:
    """Parse a CLI date value in YYYY-MM-DD format."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}. Use YYYY-MM-DD, for example 2026-06-25."
        ) from exc


def _format_ids(ids: list[int], max_items: int = 12) -> str:
    """Format created ids without flooding the terminal."""
    if not ids:
        return "-"

    visible_ids = ids[:max_items]
    suffix = "" if len(ids) <= max_items else f", ... +{len(ids) - max_items} more"
    return ", ".join(str(item) for item in visible_ids) + suffix


def _print_result(result: SeedResult) -> None:
    """Print one seed result in a readable way."""
    print(f"\n✓ Filled {result.table_name}")
    print(f"  Scenario: {result.scenario_id}")
    print(f"  Created:  {result.created_count}")
    print(f"  IDs:      {_format_ids(result.created_ids)}")

    if result.skipped_requirement_links:
        print(f"  Skipped requirement links: {result.skipped_requirement_links}")

    for note in result.notes:
        print(f"  Note: {note}")


def run_seeders(
    *,
    scenario_id: str,
    simulation_date: date | None = None,
    seed: int | None = None,
    ticket_count: int | None = None,
    config_path: str | Path | None = None,
    include_tickets: bool = True,
    include_simulation_tickets: bool = True,
) -> list[SeedResult]:
    """Run the selected simulator seeders and return their results."""
    results: list[SeedResult] = []

    shared_kwargs = {
        "scenario_id": scenario_id,
        "simulation_date": simulation_date,
        "seed": seed,
    }
    if config_path is not None:
        shared_kwargs["config_path"] = config_path

    if include_tickets:
        results.append(seed_tickets(count=ticket_count, **shared_kwargs))

    if include_simulation_tickets:
        results.append(seed_simulation_tickets(**shared_kwargs))

    return results


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m riool_service.database.simulator.seed_cli",
        description=(
            "Fill the tickets and simulation_tickets tables using one simulator scenario."
        ),
    )
    parser.add_argument(
        "--scenario",
        default="normale_dag",
        help="Scenario id from ticket_scenarios_config.json. Default: normale_dag.",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="Simulation date in YYYY-MM-DD format. Default: today.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional random seed for reproducible generated tickets.",
    )
    parser.add_argument(
        "--ticket-count",
        type=int,
        default=None,
        help="Optional exact amount for the real tickets table only.",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Optional path to another scenario JSON config file.",
    )
    parser.add_argument(
        "--only-tickets",
        action="store_true",
        help="Only fill tickets, not simulation_tickets.",
    )
    parser.add_argument(
        "--only-simulation-tickets",
        action="store_true",
        help="Only fill simulation_tickets, not tickets.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.only_tickets and args.only_simulation_tickets:
        parser.error("Use either --only-tickets or --only-simulation-tickets, not both.")

    include_tickets = not args.only_simulation_tickets
    include_simulation_tickets = not args.only_tickets

    print("Simulator seed started")
    print(f"Scenario: {args.scenario}")
    print(f"Date:     {args.date or date.today()}")
    print(f"Seed:     {args.seed if args.seed is not None else '-'}")

    results = run_seeders(
        scenario_id=args.scenario,
        simulation_date=args.date,
        seed=args.seed,
        ticket_count=args.ticket_count,
        config_path=args.config_path,
        include_tickets=include_tickets,
        include_simulation_tickets=include_simulation_tickets,
    )

    for result in results:
        _print_result(result)

    total_created = sum(result.created_count for result in results)
    print(f"\nDone. Created {total_created} tickets in total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
