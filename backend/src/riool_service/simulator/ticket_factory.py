from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from riool_service.database.models.tickets import TicketUrgency

from riool_service.simulator.config import ScenarioConfig
from riool_service.simulator.utils import choose_urgency, deadline_for, maybe_requirement_codes


@dataclass(frozen=True)
class GeneratedTicketData:
    """Database-independent generated ticket values."""

    subject_name: str
    urgency: TicketUrgency
    description: str
    created_at: datetime
    deadline_at: datetime
    requirement_codes: list[str]


def generate_ticket_data(
    *,
    rng: random.Random,
    scenario: ScenarioConfig,
    created_at: datetime,
    include_urgent: bool = True,
) -> GeneratedTicketData:
    urgency = choose_urgency(
        rng,
        scenario.percentage_urgent,
        scenario.percentage_mid_prio,
        scenario.percentage_low_prio,
    )
    if not include_urgent:
        total_percentage = scenario.percentage_mid_prio + scenario.percentage_low_prio
        if total_percentage <= 0:
            raise ValueError("At least one of mid or low priority must have a positive percentage")
        medium_percentage = int(scenario.percentage_mid_prio / total_percentage * 100)
        low_percentage = 100 - medium_percentage
        urgency = choose_urgency(rng, 0, medium_percentage, low_percentage)

    subject_name = rng.choice(scenario.subjects)
    requirement_codes = maybe_requirement_codes(rng, scenario.requirements_percentages)

    requirement_text = ", ".join(requirement_codes) if requirement_codes else "geen"
    description = (
        f"Simulator ticket voor scenario '{scenario.name}'. "
        f"Benodigdheden: {requirement_text}."
    )

    return GeneratedTicketData(
        subject_name=subject_name,
        urgency=urgency,
        description=description,
        created_at=created_at,
        deadline_at=deadline_for(created_at, urgency),
        requirement_codes=requirement_codes,
    )
