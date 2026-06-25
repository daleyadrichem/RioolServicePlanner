"""Branch seed data."""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.geocode_service import AddressCoordinates, coordinates_from_address

ORIGINAL_LOCATION: Final[dict[str, str]] = {
    "input_address": "Jac. van Looystraat 5, 5216 SB 's-Hertogenbosch",
    "formatted_address": "Jac. van Looystraat 5, 5216 SB 's-Hertogenbosch",
    "street": "Jac. van Looystraat",
    "house_number": "5",
    "city": "'s-Hertogenbosch",
}

ORIGINAL_BRANCH_NAME: Final[str] = "Branch Den Bosch"


def seed_original_branch(session: Session) -> Branch:
    """Insert or update the original Den Bosch branch."""
    location = session.scalar(
        select(Location).where(
            Location.street == ORIGINAL_LOCATION["street"],
            Location.house_number == ORIGINAL_LOCATION["house_number"],
            Location.city == ORIGINAL_LOCATION["city"],
        )
    )

    if location is None:
        coordinates = coordinates_from_address(
            ORIGINAL_LOCATION["street"],
            ORIGINAL_LOCATION["house_number"],
            ORIGINAL_LOCATION["city"],
        )
        location = _branch_location_from_coordinates(coordinates)
        session.add(location)
        session.flush()
    else:
        _fill_missing_location_fields(location)

    if location.id is None:
        session.flush()

    if location.id is None:
        raise RuntimeError("Location ID was not assigned after flushing the session.")

    branch = session.scalar(select(Branch).where(Branch.name == ORIGINAL_BRANCH_NAME))

    if branch is None:
        branch = Branch(name=ORIGINAL_BRANCH_NAME, location_id=location.id)
        session.add(branch)
    else:
        branch.location_id = location.id

    session.flush()
    print(f"Seeded branch: {branch.name} at {location.formatted_address}")
    return branch


def _branch_location_from_coordinates(coordinates: AddressCoordinates) -> Location:
    """Build the fixed branch location from a geocoded address dataclass."""
    return Location(
        longitude=coordinates.longitude,
        latitude=coordinates.latitude,
        **ORIGINAL_LOCATION,
    )


def _fill_missing_location_fields(location: Location) -> None:
    for field, value in ORIGINAL_LOCATION.items():
        current_value = getattr(location, field)
        if current_value in {None, ""}:
            setattr(location, field, value)
