"""Ticket generation service for scenario-based simulations."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency

from .repository import TicketRepository
from .scenario_types import ScenarioConfig, ScenariosFile


class TicketSimulator:
    """Generate random tickets from scenario definitions and stored locations.

    The simulator creates ticket subjects, tickets, and ticket requirements. It
    intentionally does not generate or reverse-geocode locations; reusable
    simulated locations are seeded by ``initialize_database.py``.
    """

    def __init__(
        self,
        session: Session,
        scenarios: ScenariosFile,
        seed: int | None = None,
    ) -> None:
        """Initialize the simulator with a database session and scenario config."""
        self.session = session
        self.random = random.Random(seed)
        self.repository = TicketRepository(session)
        self.scenarios = scenarios["scenarios"]
        self._scenario_by_id: dict[str, ScenarioConfig] = {
            scenario["scenario_id"]: scenario for scenario in self.scenarios
        }

    def generate_random_tickets(
        self,
        scenario_id: str,
        *,
        ticket_count: int | None = None,
        created_date: datetime | None = None,
    ) -> list[Ticket]:
        """Create random tickets for a scenario and flush them to the session.

        The caller owns transaction handling. This method adds objects to the
        session and flushes so IDs are available, but it does not commit. Every
        generated ticket in this batch receives a unique pre-seeded location.
        """
        scenario = self._get_scenario(scenario_id)
        branch = self.repository.get_branch(scenario["branch_name"])
        self._validate_branch_coordinates(branch)

        amount = ticket_count or self.random.randint(
            int(scenario["aantal_tickets_min"]),
            int(scenario["aantal_tickets_max"]),
        )
        base_date = created_date or datetime.now(timezone.utc)
        locations = self.repository.get_random_ticket_locations(
            branch=branch,
            amount=amount,
            rng=self.random,
        )

        tickets = [
            self._build_ticket(
                scenario=scenario,
                branch=branch,
                location=location,
                ticket_number=index,
                base_date=base_date,
            )
            for index, location in enumerate(locations, start=1)
        ]

        self.session.add_all(tickets)
        self.session.flush()
        return tickets

    def _build_ticket(
        self,
        *,
        scenario: ScenarioConfig,
        branch: Branch,
        location: Location,
        ticket_number: int,
        base_date: datetime,
    ) -> Ticket:
        """Build one ticket using an already stored unique location."""
        created_at = self._random_datetime_in_day(
            base_date=base_date,
            start_time=scenario["dag_start_tijd"],
            end_time=scenario["dag_end_tijd"],
        )
        urgency = self._random_urgency(scenario)
        subjects = scenario.get("subjects") or ["Algemene storing"]
        subject_name = self.random.choice(subjects)
        subject = self.repository.get_or_create_subject(subject_name)

        print(f"Ticket {ticket_number}: {location.formatted_address}")

        ticket = Ticket(
            branch=branch,
            location=location,
            subject=subject,
            description=self._build_description(scenario, location),
            urgency=urgency,
            status=TicketStatus.OPEN,
            created_at=created_at,
            deadline_at=self._deadline_for(created_at, urgency),
        )
        for requirement in self._random_requirements(scenario):
            ticket.ticket_requirements.append(
                TicketRequirement(requirement=requirement)
            )
        return ticket

    def _get_scenario(self, scenario_id: str) -> ScenarioConfig:
        """Return the scenario for ``scenario_id`` or raise a helpful error."""
        try:
            return self._scenario_by_id[scenario_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._scenario_by_id))
            raise ValueError(
                f"Unknown scenario_id {scenario_id!r}. Known scenarios: {known}"
            ) from exc

    @staticmethod
    def _validate_branch_coordinates(branch: Branch) -> None:
        """Validate that a branch has usable latitude and longitude values."""
        if (
            branch.location is None
            or branch.location.latitude is None
            or branch.location.longitude is None
        ):
            raise ValueError(
                f"Branch {branch.name!r} must have a location with latitude and longitude"
            )

    def _random_urgency(self, scenario: ScenarioConfig) -> TicketUrgency:
        """Choose a ticket urgency using scenario percentage weights."""
        choices = [TicketUrgency.URGENT, TicketUrgency.MEDIUM, TicketUrgency.LOW]
        weights = [
            float(scenario["percentage_urgent"]),
            float(scenario["percentage_mid_prio"]),
            float(scenario["percentage_laag_prio"]),
        ]
        return self.random.choices(choices, weights=weights, k=1)[0]

    def _random_requirements(self, scenario: ScenarioConfig) -> list[Requirement]:
        """Choose requirements using scenario percentage probabilities."""
        selected: list[Requirement] = []
        requirement_percentages = scenario.get("requirements_percentages") or {}
        for code, percentage in requirement_percentages.items():
            if self.random.uniform(0, 100) <= float(percentage):
                selected.append(self.repository.get_requirement(str(code)))
        return selected

    def _random_datetime_in_day(
        self,
        base_date: datetime,
        start_time: str,
        end_time: str,
    ) -> datetime:
        """Return a random datetime between two ``HH:MM`` times on ``base_date``."""
        if base_date.tzinfo is None:
            base_date = base_date.replace(tzinfo=timezone.utc)

        start_hour, start_minute = map(int, start_time.split(":"))
        end_hour, end_minute = map(int, end_time.split(":"))
        start_dt = base_date.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        )
        end_dt = base_date.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )

        if end_dt <= start_dt:
            raise ValueError("dag_end_tijd must be later than dag_start_tijd")

        seconds = int((end_dt - start_dt).total_seconds())
        return start_dt + timedelta(seconds=self.random.randint(0, seconds))

    @staticmethod
    def _deadline_for(created_at: datetime, urgency: TicketUrgency) -> datetime:
        """Calculate a deadline based on the ticket urgency."""
        if urgency == TicketUrgency.URGENT:
            return created_at + timedelta(hours=8)
        if urgency == TicketUrgency.MEDIUM:
            return created_at + timedelta(days=2)
        return created_at + timedelta(days=3)

    @staticmethod
    def _build_description(scenario: ScenarioConfig, location: Location) -> str:
        """Build a deterministic description for a generated ticket."""
        return (
            f"Generated by scenario {scenario['scenario_id']}. "
            f"Coordinates: lat={float(location.latitude):.6f}, "
            f"lon={float(location.longitude):.6f}."
        )
