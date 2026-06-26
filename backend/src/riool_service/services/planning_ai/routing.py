from __future__ import annotations

from sqlalchemy.orm import Session

from riool_service.database.models.location import Location
from riool_service.services.planning_ai.models import RouteMatrix, TechnicianInput, TicketInput
from riool_service.services.routing.models import RoutePoint
from riool_service.services.routing import service as routing_service


class PlanningRoutingError(ValueError):
    pass


def get_planning_route_matrix(
    session: Session,
    technicians: list[TechnicianInput],
    tickets: list[TicketInput],
    *,
    refresh_cache: bool = False,
) -> RouteMatrix:
    """Fetch a full location matrix through the internal OSRM provider/cache.

    This deliberately does not call the FastAPI routing endpoints. It reuses the
    same internal routing provider/cache that the endpoints use.
    """
    location_ids = sorted(
        {
            *[technician.start_location_id for technician in technicians],
            *[technician.end_location_id for technician in technicians],
            *[ticket.location_id for ticket in tickets],
        }
    )
    if len(location_ids) < 2:
        return RouteMatrix(travel_minutes={}, distance_km={})
    if len(location_ids) > 100:
        raise PlanningRoutingError(f"OSRM supports at most 100 unique locations per matrix; got {len(location_ids)}")

    locations = list(session.query(Location).filter(Location.id.in_(location_ids)).all())
    by_id = {location.id: location for location in locations}
    missing = [location_id for location_id in location_ids if location_id not in by_id]
    if missing:
        raise PlanningRoutingError(f"Missing route locations: {missing}")

    points: list[RoutePoint] = []
    for location_id in location_ids:
        try:
            points.append(RoutePoint.from_location(by_id[location_id]))
        except ValueError as exc:
            raise PlanningRoutingError(f"Location {location_id} has no usable coordinates") from exc

    try:
        legs = routing_service._get_or_fetch_legs(session, points, refresh_cache=refresh_cache)  # noqa: SLF001
    except Exception as exc:
        raise PlanningRoutingError(str(exc)) from exc

    travel_minutes: dict[tuple[int, int], int] = {}
    distance_km: dict[tuple[int, int], float] = {}
    for from_id in location_ids:
        for to_id in location_ids:
            if from_id == to_id:
                travel_minutes[(from_id, to_id)] = 0
                distance_km[(from_id, to_id)] = 0.0
                continue
            leg = legs.get((from_id, to_id))
            if leg is None:
                raise PlanningRoutingError(f"No route found between location {from_id} and {to_id}")
            travel_minutes[(from_id, to_id)] = leg.travel_minutes
            distance_km[(from_id, to_id)] = leg.distance_km
    return RouteMatrix(travel_minutes=travel_minutes, distance_km=distance_km)
