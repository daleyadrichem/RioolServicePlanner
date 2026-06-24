"""Geographic randomization helpers."""

from __future__ import annotations

import math
import random

EARTH_RADIUS_KM = 6371.0088


def random_coordinates_within_radius(
    *,
    rng: random.Random,
    latitude: float,
    longitude: float,
    radius_km: float,
) -> tuple[float, float]:
    """Return a uniformly random latitude/longitude within ``radius_km``."""
    bearing = rng.uniform(0, 2 * math.pi)
    distance_km = radius_km * math.sqrt(rng.random())
    angular_distance = distance_km / EARTH_RADIUS_KM

    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )

    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    lon2 = (lon2 + 3 * math.pi) % (2 * math.pi) - math.pi

    return math.degrees(lat2), math.degrees(lon2)
