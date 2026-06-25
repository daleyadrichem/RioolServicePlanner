from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from geopy.extra.rate_limiter import RateLimiter  # type: ignore[import-untyped]
from geopy.geocoders import Nominatim  # type: ignore[import-untyped]


__all__ = [
    "AddressCoordinates",
    "CoordinatesAddress",
    "coordinates_from_address",
    "address_from_coordinates",
]


_GeocodeStatus = Literal["resolved", "not_found"]


@dataclass(frozen=True)
class AddressCoordinates:
    """Address parts with optional latitude and longitude coordinates."""

    street: str
    house_number: str
    city: str
    latitude: float | None
    longitude: float | None
    status: _GeocodeStatus


@dataclass(frozen=True)
class CoordinatesAddress:
    """Coordinates with optional resolved address parts."""

    latitude: float
    longitude: float
    street: str | None
    house_number: str | None
    city: str | None
    status: _GeocodeStatus


_geolocator = Nominatim(user_agent="nxtphase-sewer-planning-case")

_geocode = RateLimiter(_geolocator.geocode, min_delay_seconds=2, max_retries=5)

_reverse_geocode = RateLimiter(_geolocator.reverse, min_delay_seconds=2, max_retries=5)


def coordinates_from_address(
    street: str, house_number: str, city: str
) -> AddressCoordinates:
    """Resolve an address to latitude and longitude coordinates.

    Parameters
    ----------
    street : str
        Street name.
    house_number : str
        House number.
    city : str
        City name.

    Returns
    -------
    AddressCoordinates
        Address parts, latitude, longitude, and resolution status
    """
    street = street.strip()
    house_number = house_number.strip()
    city = city.strip()
    address = _format_address(street, house_number, city)
    location = _geocode(address)

    if location is None:
        return AddressCoordinates(
            street=street,
            house_number=house_number,
            city=city,
            latitude=None,
            longitude=None,
            status="not_found",
        )

    return AddressCoordinates(
        street=street,
        house_number=house_number,
        city=city,
        latitude=float(location.latitude),
        longitude=float(location.longitude),
        status="resolved",
    )


def address_from_coordinates(
    latitude: float,
    longitude: float,
) -> CoordinatesAddress:
    """Resolve latitude and longitude coordinates to address parts.

    Parameters
    ----------
    latitude : float
        Latitude coordinate.
    longitude : float
        Longitude coordinate.

    Returns
    -------
    CoordinatesAddress
        Latitude, longitude, address parts, and resolution status.
    """
    location = _reverse_geocode(
        query=(latitude, longitude),
        exactly_one=True,
        language="en",
        addressdetails=True,
    )

    if location is None:
        return CoordinatesAddress(
            latitude=latitude,
            longitude=longitude,
            street=None,
            house_number=None,
            city=None,
            status="not_found",
        )

    address = location.raw.get("address", {})

    return CoordinatesAddress(
        latitude=latitude,
        longitude=longitude,
        street=address.get("road"),
        house_number=address.get("house_number"),
        city=_get_city(address),
        status="resolved",
    )


def _format_address(
    street: str,
    house_number: str,
    city: str,
) -> str:
    """Format address parts for geocoding."""
    return f"{street} {house_number}, {city}"


def _get_city(address: dict[str, str]) -> str | None:
    """Extract city-like value from a reverse geocoded address."""
    return (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
    )
