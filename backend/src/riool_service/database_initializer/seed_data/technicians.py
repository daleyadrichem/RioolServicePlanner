"""Technician seed data."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.technician import Technician, TechnicianStatus
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.geocode_service import AddressCoordinates, coordinates_from_address

_ADDRESS_PATTERN = re.compile(
    r"^\s*(?P<street>.+?)\s+(?P<house_number>\d+[\w\-/]*)\s*,\s*"
    r"(?P<city>[^,]+?)"
    r"(?:\s*,\s*(?P<country>[^,]+))?\s*$"
)
DEFAULT_COUNTRY = "Nederland"


def seed_technicians(session: Session, config: dict[str, Any]) -> None:
    """Insert or update technicians and their capability requirements."""
    for branch_config in config.get("branches", []):
        branch_name = branch_config["branch_name"]
        branch = session.scalar(select(Branch).where(Branch.name == branch_name))
        if branch is None:
            raise ValueError(
                f"Branch {branch_name!r} was not found for technician seed"
            )

        for technician_data in branch_config.get("technicians", []):
            technician = session.scalar(
                select(Technician).where(
                    Technician.branch_id == branch.id,
                    Technician.name == technician_data["name"],
                )
            )

            status_value = technician_data.get("status", TechnicianStatus.ACTIVE.value)
            status = TechnicianStatus(status_value)
            workday_start = _time_to_minutes(
                technician_data.get("workday_start", "08:00")
            )
            workday_end = _time_to_minutes(technician_data.get("workday_end", "17:00"))
            home_location = _home_location_from_config(session, technician_data)

            if technician is None:
                technician = Technician(
                    branch_id=branch.id,
                    name=technician_data["name"],
                    home_location_id=home_location.id if home_location is not None else None,
                    status=status,
                    workday_start_minutes=workday_start,
                    workday_end_minutes=workday_end,
                )
                session.add(technician)
                session.flush()
            else:
                technician.status = status
                technician.home_location_id = home_location.id if home_location is not None else None
                technician.workday_start_minutes = workday_start
                technician.workday_end_minutes = workday_end

            _replace_technician_requirements(
                session=session,
                technician=technician,
                requirement_codes=technician_data.get("requirements", []),
            )

            start_location = home_location.formatted_address if home_location else branch.location.formatted_address
            print(
                f"Seeded technician: {technician.name} "
                f"({', '.join(technician_data.get('requirements', [])) or 'no requirements'}), "
                f"start/end location: {start_location}"
            )


def _home_location_from_config(
    session: Session,
    technician_data: dict[str, Any],
) -> Location | None:
    home_address = str(technician_data.get("home_address") or "").strip()
    if not home_address:
        return None

    parsed = _parse_home_address(home_address)
    formatted_address = f'{parsed["street"]} {parsed["house_number"]}, {parsed["city"]}'

    existing = session.scalar(
        select(Location).where(
            (func.lower(Location.formatted_address) == formatted_address.lower())
            | (func.lower(Location.input_address) == formatted_address.lower())
            | (
                (func.lower(Location.street) == parsed["street"].lower())
                & (func.lower(Location.house_number) == parsed["house_number"].lower())
                & (func.lower(Location.city) == parsed["city"].lower())
            )
        )
    )
    if existing is not None:
        return existing

    try:
        coordinates = coordinates_from_address(
            parsed["street"],
            parsed["house_number"],
            f'{parsed["city"]}, {parsed["country"]}',
        )
    except Exception as exc:  # pragma: no cover - depends on external geocoder availability
        raise ValueError(
            f"Could not geocode home address {home_address!r}. "
            "Check your internet connection or provide a valid address."
        ) from exc

    if coordinates.status != "resolved" or coordinates.latitude is None or coordinates.longitude is None:
        raise ValueError(f"Home address {home_address!r} could not be found")

    location = _location_from_coordinates(
        input_address=home_address,
        formatted_address=formatted_address,
        parsed=parsed,
        coordinates=coordinates,
    )
    session.add(location)
    session.flush()
    return location


def _parse_home_address(address: str) -> dict[str, str]:
    match = _ADDRESS_PATTERN.match(address)
    if match is None:
        raise ValueError(
            "Technician home_address must use format "
            "'street house_number, city' or 'street house_number, city, country'"
        )

    parsed = {key: (value or "").strip() for key, value in match.groupdict().items()}
    parsed["country"] = parsed.get("country") or DEFAULT_COUNTRY
    return parsed


def _location_from_coordinates(
    *,
    input_address: str,
    formatted_address: str,
    parsed: dict[str, str],
    coordinates: AddressCoordinates,
) -> Location:
    return Location(
        input_address=input_address,
        formatted_address=formatted_address,
        street=parsed["street"],
        house_number=parsed["house_number"],
        city=parsed["city"],
        latitude=float(coordinates.latitude),
        longitude=float(coordinates.longitude),
    )


def _replace_technician_requirements(
    *,
    session: Session,
    technician: Technician,
    requirement_codes: list[str],
) -> None:
    session.query(TechnicianRequirement).filter(
        TechnicianRequirement.technician_id == technician.id
    ).delete(synchronize_session=False)

    for code in requirement_codes:
        requirement = session.scalar(
            select(Requirement).where(func.lower(Requirement.code) == str(code).lower())
        )
        if requirement is None:
            raise ValueError(f"Requirement code {code!r} was not found")

        session.add(
            TechnicianRequirement(
                technician_id=technician.id,
                requirement_id=requirement.id,
            )
        )


def _time_to_minutes(value: str) -> int:
    hours, minutes = map(int, value.split(":"))
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise ValueError(f"Invalid time value {value!r}")
    return hours * 60 + minutes
