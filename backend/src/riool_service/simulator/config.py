from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from pydantic import BaseModel, Field


TICKET_SCENARIOS_CONFIG_ENV_VAR: Final[str] = "TICKET_SCENARIOS_CONFIG_PATH"
DEFAULT_TICKET_SCENARIOS_CONFIG_PATH: Final[str] = "ticket_scenario_config.json"


class ScenarioConfig(BaseModel):
    """Normalized scenario settings used by the simulator."""

    scenario_id: str
    name: str = Field(alias="naam")
    branch_name: str

    day_start_time: str = Field(default="08:00", alias="dag_start_tijd")
    day_end_time: str = Field(default="17:00", alias="dag_end_tijd")

    percentage_urgent: int = 25
    percentage_mid_prio: int = 30
    percentage_low_prio: int = Field(default=45, alias="percentage_laag_prio")

    requirements_percentages: dict[str, int] = Field(default_factory=dict)

    ticket_count_min: int = Field(default=18, alias="aantal_tickets_min")
    ticket_count_max: int = Field(default=30, alias="aantal_tickets_max")

    simulation_ticket_count_min: int = Field(
        default=30,
        alias="aantal_simulation_tickets_min",
    )
    simulation_ticket_count_max: int = Field(
        default=50,
        alias="aantal_simulation_tickets_max",
    )
    
    seed: int | None = None
    subjects: list[str] = Field(default_factory=list)
    simulation_ticket_submission_schedule: list[dict[str, int]] = Field(
        default_factory=list,
        alias="simulation_ticket_inzend_planning",
    )

    model_config = {
        "populate_by_name": True,
    }


def get_config_path(config_path: str | Path | None = None) -> Path:
    """Return the scenario config path from an explicit value, .env, or default."""
    load_dotenv()

    resolved_path = (
        config_path
        or os.getenv(TICKET_SCENARIOS_CONFIG_ENV_VAR)
        or DEFAULT_TICKET_SCENARIOS_CONFIG_PATH
    )

    return Path(resolved_path)


def load_scenarios(config_path: str | Path | None = None) -> dict[str, ScenarioConfig]:
    """Load all simulator scenarios from JSON."""
    path = get_config_path(config_path)

    with path.open("r", encoding="utf-8") as file:
        raw_config = json.load(file)

    scenarios = [ScenarioConfig.model_validate(item) for item in raw_config["scenarios"]]
    return {scenario.scenario_id: scenario for scenario in scenarios}


def get_scenario(
    scenario_id: str = "normale_dag",
    config_path: str | Path | None = None,
) -> ScenarioConfig:
    """Return one scenario by id."""
    scenarios = load_scenarios(config_path)

    try:
        return scenarios[scenario_id]
    except KeyError as exc:
        available = ", ".join(sorted(scenarios))
        raise ValueError(
            f"Unknown scenario_id {scenario_id!r}. Available scenarios: {available}"
        ) from exc