"""Reusable simulated location seed data."""

from __future__ import annotations

import math
import random
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.geocode_service import CoordinatesAddress, address_from_coordinates

DEFAULT_LOCATION_GENERATION_SEED: Final[int] = 42
DEFAULT_MAX_LOCATION_ATTEMPTS_MULTIPLIER: Final[int] = 20


def seed_simulated_locations(session: Session, config: dict[str, Any]) -> None:
    """Seed reusable random ticket locations from a location-generation config."""
    branches = config.get("branches", [])
    if not branches:
        print("No simulated locations config found; skipping location seed.")
        return

    rng = random.Random(int(config.get("seed", DEFAULT_LOCATION_GENERATION_SEED)))

    for branch_config in branches:
        branch_name = str(branch_config["branch_name"])
        target_count = int(branch_config.get("count", 0))
        radius_km = float(branch_config["radius_km"])
        max_attempts = int(
            branch_config.get(
                "max_attempts",
                max(
                    target_count * DEFAULT_MAX_LOCATION_ATTEMPTS_MULTIPLIER,
                    target_count,
                ),
            )
        )

        if target_count <= 0:
            continue

        branch = session.scalar(select(Branch).where(Branch.name == branch_name))
        if branch is None:
            raise ValueError(f"Branch {branch_name!r} was not found for location seed")
        _validate_branch_coordinates(branch)

        existing_count = _count_simulated_locations_for_branch(session, branch_name)
        needed = max(0, target_count - existing_count)
        if needed == 0:
            print(
                f"Seeded simulated locations for {branch_name}: already have "
                f"{existing_count}/{target_count}"
            )
            continue

        created = 0
        attempts = 0
        seen_addresses = _known_formatted_addresses(session)

        while created < needed and attempts < max_attempts:
            latitude, longitude = _random_coordinates_within_radius(
                rng=rng,
                latitude=float(branch.location.latitude),
                longitude=float(branch.location.longitude),
                radius_km=radius_km,
            )
            address = address_from_coordinates(latitude, longitude)
            location = _simulated_location_from_address(
                address=address,
                branch_name=branch_name,
                seen_addresses=seen_addresses,
            )
            if location is None:
                continue

            session.add(location)
            session.flush()
            created += 1
            print(
                f"Created simulated location {created}/{needed} for {branch_name}: {location.formatted_address}"
            )

        if created < needed:
            raise RuntimeError(
                f"Only seeded {created} of {needed} missing locations for {branch_name!r} "
                f"after {attempts} attempts. Try increasing radius_km or max_attempts."
            )

        print(
            f"Seeded simulated locations for {branch_name}: "
            f"created {created}, total target {target_count}"
        )


def _simulated_location_from_address(
    *,
    address: CoordinatesAddress,
    branch_name: str,
    seen_addresses: set[str],
) -> Location | None:
    """Build a simulated Location from a reverse-geocoded dataclass."""
    if (
        address.status == "not_found"
        or address.street is None
        or address.house_number is None
        or address.city is None
    ):
        return None

    formatted_address = f"{address.street} {address.house_number}, {address.city}"
    if formatted_address in seen_addresses:
        return None

    seen_addresses.add(formatted_address)
    return Location(
        input_address=f"Simulated address near {branch_name}",
        formatted_address=formatted_address,
        street=address.street,
        house_number=address.house_number,
        city=address.city,
        latitude=address.latitude,
        longitude=address.longitude,
    )


def _count_simulated_locations_for_branch(session: Session, branch_name: str) -> int:
    return int(
        session.query(Location)
        .filter(Location.input_address == f"Simulated address near {branch_name}")
        .count()
    )


def _known_formatted_addresses(session: Session) -> set[str]:
    return {
        row[0]
        for row in session.query(Location.formatted_address)
        .filter(Location.formatted_address.isnot(None))
        .all()
    }


def _validate_branch_coordinates(branch: Branch) -> None:
    if (
        branch.location is None
        or branch.location.latitude is None
        or branch.location.longitude is None
    ):
        raise ValueError(
            f"Branch {branch.name!r} must have a location with latitude and longitude"
        )


def _random_coordinates_within_radius(
    *,
    rng: random.Random,
    latitude: float,
    longitude: float,
    radius_km: float,
) -> tuple[float, float]:
    """Return uniformly distributed coordinates within ``radius_km``."""
    earth_radius_km = 6371.0
    distance = radius_km * math.sqrt(rng.random())
    bearing = rng.uniform(0, 2 * math.pi)

    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    angular_distance = distance / earth_radius_km

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )

    return math.degrees(lat2), math.degrees(lon2)
