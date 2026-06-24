"""Configuration loading for database initialization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final, cast

from dotenv import load_dotenv

from riool_service.database.db_utils import get_database_url

TECHNICIANS_CONFIG_ENV_VAR: Final[str] = "TECHNICIANS_CONFIG_PATH"
DEFAULT_TECHNICIANS_CONFIG_PATH: Final[str] = "technicians_config.json"

LOCATIONS_CONFIG_ENV_VAR: Final[str] = "LOCATIONS_CONFIG_PATH"
DEFAULT_LOCATIONS_CONFIG_PATH: Final[str] = "locations_config.json"


def load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    """Load a JSON object from ``path`` with a helpful validation error."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"{label} config {config_path} was not found.")

    with config_path.open(encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            f"{label} config {config_path} must contain a JSON object at the top level."
        )

    return cast(dict[str, Any], data)


def load_optional_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    """Load a JSON object when it exists, otherwise return an empty config."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    return load_json_object(config_path, label=label)


def load_initializer_settings() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Load environment, database URL, technician config and location config."""
    load_dotenv()
    database_url = get_database_url()
    technicians_config_path = os.getenv(
        TECHNICIANS_CONFIG_ENV_VAR,
        DEFAULT_TECHNICIANS_CONFIG_PATH,
    )
    locations_config_path = os.getenv(
        LOCATIONS_CONFIG_ENV_VAR,
        DEFAULT_LOCATIONS_CONFIG_PATH,
    )

    technicians_config = load_json_object(
        technicians_config_path,
        label="Technician",
    )
    locations_config = load_optional_json_object(
        locations_config_path,
        label="Locations",
    )
    return database_url, technicians_config, locations_config
