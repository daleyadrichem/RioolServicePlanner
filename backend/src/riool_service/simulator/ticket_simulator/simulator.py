"""Ticket generation service for scenario-based simulations."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.tickets import Ticket, TicketStatus, TicketUrgency
from riool_service.geocode_service import address_from_coordinates

from .geocoding_types import ResolvedAddress
from .geometry import random_coordinates_within_radius
from .repository import TicketRepository
from .scenario_types import ScenarioConfig, ScenariosFile


class TicketSimulator:
    """Generate random ticket input data from scenario definitions.

    The simulator creates locations, ticket subjects, tickets, and ticket
    requirements. It does not create technicians, planning runs, route caches,
    or planning assignments; those remain planner responsibilities.
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
        session and flushes so IDs are available, but it does not commit.
        """
        scenario = self._get_scenario(scenario_id)
        branch = self.repository.get_branch(scenario["branch_name"])
        self._validate_branch_coordinates(branch)

        amount = ticket_count or self.random.randint(
            int(scenario["aantal_tickets_min"]),
            int(scenario["aantal_tickets_max"]),
        )
        base_date = created_date or datetime.now(timezone.utc)

        tickets: list[Ticket] = []
        attempts = 0
        max_attempts = amount * 10

        while len(tickets) < amount and attempts < max_attempts:
            attempts += 1
            ticket = self._try_generate_ticket(
                scenario=scenario,
                branch=branch,
                ticket_number=len(tickets) + 1,
                base_date=base_date,
            )
            if ticket is None:
                continue

            self.session.add(ticket)
            tickets.append(ticket)

        if len(tickets) < amount:
            raise RuntimeError(
                f"Only generated {len(tickets)} of {amount} tickets after "
                f"{max_attempts} attempts. Geocoding may be returning too many "
                "'not_found' results."
            )

        self.session.flush()
        return tickets

    def _try_generate_ticket(
        self,
        *,
        scenario: ScenarioConfig,
        branch: Branch,
        ticket_number: int,
        base_date: datetime,
    ) -> Ticket | None:
        """Build one ticket, returning ``None`` when reverse geocoding fails."""
        created_at = self._random_datetime_in_day(
            base_date=base_date,
            start_time=scenario["dag_start_tijd"],
            end_time=scenario["dag_end_tijd"],
        )
        urgency = self._random_urgency(scenario)
        latitude, longitude = random_coordinates_within_radius(
            rng=self.random,
            latitude=float(branch.location.latitude),
            longitude=float(branch.location.longitude),
            radius_km=float(scenario["radius_km"]),
        )
        address = cast(ResolvedAddress, address_from_coordinates(latitude, longitude))

        if address.status == "not_found" or address.house_number is None:
            return None

        subjects = scenario.get("subjects") or ["Algemene storing"]
        subject_name = self.random.choice(subjects)
        subject = self.repository.get_or_create_subject(subject_name)
        formatted_address = self._format_address(address)

        print(f"Ticket {ticket_number}: {formatted_address}")

        location = self.repository.get_or_create_location(
            scenario=scenario,
            ticket_number=ticket_number,
            formatted_address=formatted_address,
            address=address,
            latitude=latitude,
            longitude=longitude,
        )

        ticket = Ticket(
            branch=branch,
            location=location,
            subject=subject,
            description=self._build_description(scenario, latitude, longitude),
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
    def _format_address(address: ResolvedAddress) -> str:
        """Format a resolved address for storage and duplicate lookup."""
        return f"{address.street} {address.house_number}, {address.city}, {address.country}"

    @staticmethod
    def _build_description(
        scenario: ScenarioConfig,
        latitude: float,
        longitude: float,
    ) -> str:
        """Build a deterministic description for a generated ticket."""
        return (
            f"Generated by scenario {scenario['scenario_id']}. "
            f"Coordinates: lat={latitude:.6f}, lon={longitude:.6f}."
        )
