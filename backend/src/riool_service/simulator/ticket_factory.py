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
) -> GeneratedTicketData:
    urgency = choose_urgency(
        rng,
        scenario.percentage_urgent,
        scenario.percentage_mid_prio,
        scenario.percentage_low_prio,
    )
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
