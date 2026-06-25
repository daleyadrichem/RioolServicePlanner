from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from riool_service.services.routing.models import RouteLeg, RoutePoint

DEFAULT_OSRM_BASE_URL = "https://router.project-osrm.org"
DEFAULT_PROFILE = "driving"
DEFAULT_TIMEOUT_SECONDS = 15
MAX_TABLE_POINTS = 100


class OsrmProviderError(RuntimeError):
    """Raised when OSRM cannot return a usable route response."""


@dataclass(frozen=True)
class OsrmProvider:
    """Small HTTP client for OSRM's public or self-hosted HTTP API."""

    base_url: str = DEFAULT_OSRM_BASE_URL
    profile: str = DEFAULT_PROFILE
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "OsrmProvider":
        return cls(
            base_url=os.getenv("OSRM_BASE_URL", DEFAULT_OSRM_BASE_URL).rstrip("/"),
            profile=os.getenv("OSRM_PROFILE", DEFAULT_PROFILE).strip() or DEFAULT_PROFILE,
            timeout_seconds=int(os.getenv("OSRM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
        )

    def table(self, points: list[RoutePoint]) -> dict[tuple[int, int], RouteLeg]:
        """Return fastest-route duration and distance for every point pair.

        Uses OSRM's table service in a single request. Durations are seconds in
        OSRM, distances are meters; this method converts them to minutes and km.
        """
        if len(points) < 2:
            return {}
        if len(points) > MAX_TABLE_POINTS:
            raise OsrmProviderError(
                f"OSRM table request supports at most {MAX_TABLE_POINTS} points in this module; got {len(points)}"
            )

        coordinates = ";".join(point.osrm_coordinate() for point in points)
        query = urlencode({"annotations": "duration,distance"})
        url = f"{self.base_url}/table/v1/{self.profile}/{coordinates}?{query}"
        payload = self._get_json(url)

        if payload.get("code") != "Ok":
            raise OsrmProviderError(f"OSRM table failed: {payload.get('message') or payload.get('code')}")

        durations = payload.get("durations")
        distances = payload.get("distances")
        if not isinstance(durations, list) or not isinstance(distances, list):
            raise OsrmProviderError("OSRM table response did not include durations and distances")

        legs: dict[tuple[int, int], RouteLeg] = {}
        for from_index, from_point in enumerate(points):
            for to_index, to_point in enumerate(points):
                if from_index == to_index:
                    continue
                duration_seconds = durations[from_index][to_index]
                distance_meters = distances[from_index][to_index]
                if duration_seconds is None or distance_meters is None:
                    continue
                travel_minutes = max(1, round(float(duration_seconds) / 60))
                distance_km = float(distance_meters) / 1000
                legs[(from_point.id, to_point.id)] = RouteLeg(
                    from_location_id=from_point.id,
                    to_location_id=to_point.id,
                    travel_minutes=travel_minutes,
                    distance_km=distance_km,
                )
        return legs

    def table_between(
        self,
        sources: list[RoutePoint],
        destinations: list[RoutePoint],
    ) -> dict[tuple[int, int], RouteLeg]:
        """Return fastest-route duration and distance from sources to destinations.

        This uses OSRM's table service with explicit ``sources`` and
        ``destinations`` indices, so callers do not have to request a full
        NxN matrix when only a subset of pairs is needed. Durations are
        returned by OSRM in seconds and distances in meters; this method
        converts them to minutes and km.
        """
        if not sources or not destinations:
            return {}

        unique_points: list[RoutePoint] = []
        index_by_location_id: dict[int, int] = {}
        for point in [*sources, *destinations]:
            if point.id not in index_by_location_id:
                index_by_location_id[point.id] = len(unique_points)
                unique_points.append(point)

        if len(unique_points) > MAX_TABLE_POINTS:
            raise OsrmProviderError(
                f"OSRM table request supports at most {MAX_TABLE_POINTS} unique points in this module; "
                f"got {len(unique_points)}"
            )

        source_indices = [index_by_location_id[point.id] for point in sources]
        destination_indices = [index_by_location_id[point.id] for point in destinations]
        coordinates = ";".join(point.osrm_coordinate() for point in unique_points)
        query = urlencode(
            {
                "annotations": "duration,distance",
                "sources": ";".join(str(index) for index in source_indices),
                "destinations": ";".join(str(index) for index in destination_indices),
            }
        )
        url = f"{self.base_url}/table/v1/{self.profile}/{coordinates}?{query}"
        payload = self._get_json(url)

        if payload.get("code") != "Ok":
            raise OsrmProviderError(f"OSRM table failed: {payload.get('message') or payload.get('code')}")

        durations = payload.get("durations")
        distances = payload.get("distances")
        if not isinstance(durations, list) or not isinstance(distances, list):
            raise OsrmProviderError("OSRM table response did not include durations and distances")

        legs: dict[tuple[int, int], RouteLeg] = {}
        for source_row_index, source_point in enumerate(sources):
            for destination_column_index, destination_point in enumerate(destinations):
                if source_point.id == destination_point.id:
                    continue
                duration_seconds = durations[source_row_index][destination_column_index]
                distance_meters = distances[source_row_index][destination_column_index]
                if duration_seconds is None or distance_meters is None:
                    continue
                travel_minutes = max(1, round(float(duration_seconds) / 60))
                distance_km = float(distance_meters) / 1000
                legs[(source_point.id, destination_point.id)] = RouteLeg(
                    from_location_id=source_point.id,
                    to_location_id=destination_point.id,
                    travel_minutes=travel_minutes,
                    distance_km=distance_km,
                )
        return legs

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "riool-service-planner/0.1"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise OsrmProviderError(f"OSRM HTTP {exc.code}: {body[:300]}") from exc
        except URLError as exc:
            raise OsrmProviderError(f"Could not reach OSRM: {exc.reason}") from exc
        except TimeoutError as exc:
            raise OsrmProviderError("OSRM request timed out") from exc
        except json.JSONDecodeError as exc:
            raise OsrmProviderError("OSRM response was not valid JSON") from exc
