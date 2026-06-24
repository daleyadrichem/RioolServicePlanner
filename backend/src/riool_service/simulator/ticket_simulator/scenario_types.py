"""Typed structures for ticket generation scenario configuration."""

from __future__ import annotations

from typing import TypedDict


class ScenarioConfig(TypedDict):
    """Configuration for one ticket generation scenario."""

    scenario_id: str
    branch_name: str
    aantal_tickets_min: int | str
    aantal_tickets_max: int | str
    dag_start_tijd: str
    dag_end_tijd: str
    radius_km: float | int | str
    percentage_urgent: float | int | str
    percentage_mid_prio: float | int | str
    percentage_laag_prio: float | int | str
    subjects: list[str] | None
    requirements_percentages: dict[str, float | int | str] | None


class ScenariosFile(TypedDict):
    """Top-level scenarios JSON document."""

    scenarios: list[ScenarioConfig]
