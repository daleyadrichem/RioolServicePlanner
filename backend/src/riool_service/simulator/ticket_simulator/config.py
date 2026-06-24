"""Configuration loading helpers for ticket scenario simulation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, cast

from .scenario_types import ScenariosFile

SCENARIOS_CONFIG_ENV_VAR: Final[str] = "TICKET_SCENARIOS_CONFIG_PATH"
DEFAULT_SCENARIOS_CONFIG_PATH: Final[str] = "scenarios.json"


def load_scenarios(path: str | Path) -> ScenariosFile:
    """Load scenario definitions from a JSON file."""
    with Path(path).open("r", encoding="utf-8") as file:
        return cast(ScenariosFile, json.load(file))


def parse_created_date(value: str | None) -> datetime | None:
    """Parse a CLI date argument and default naive datetimes to UTC."""
    if value is None:
        return None

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
