from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from riool_service.database.models.location import Location
from riool_service.database.models.route_cache import RouteCache, RouteProvider
from riool_service.services.planning_ai.models import RouteMatrix, TechnicianInput, TicketInput
from riool_service.services.routing.models import RoutePoint
from riool_service.services.routing import service as routing_service


logger = logging.getLogger(__name__)


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
            *[technician.office_location_id for technician in technicians],
            *[ticket.location_id for ticket in tickets],
        }
    )
    logger.debug(
        "Planning route matrix requested as FULL matrix: technicians=%s tickets=%s unique_locations=%s refresh_cache=%s",
        len(technicians),
        len(tickets),
        len(location_ids),
        refresh_cache,
    )
    if len(location_ids) < 2:
        logger.debug("Planning route matrix has fewer than 2 locations; returning empty matrix")
        return RouteMatrix(travel_minutes={}, distance_km={})

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
        logger.debug("Planning route matrix fetching/loading complete matrix for %s point(s)", len(points))
        legs = _get_or_fetch_complete_matrix(session, points, refresh_cache=refresh_cache)
        logger.debug("Planning route matrix complete matrix loaded with %s directed cached/fetched leg(s)", len(legs))
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



def get_cached_planning_route_matrix(
    session: Session,
    technicians: list[TechnicianInput],
    tickets: list[TicketInput],
) -> RouteMatrix:
    """Load a complete planner matrix from the database only.

    This is used by the daytime operational replanner. It must not call OSRM or
    any external routing provider; all directed pairs are expected to already be
    present in route_cache.
    """
    location_ids = sorted(
        {
            *[technician.start_location_id for technician in technicians],
            *[technician.end_location_id for technician in technicians],
            *[technician.office_location_id for technician in technicians],
            *[ticket.location_id for ticket in tickets],
        }
    )
    logger.debug(
        "Cached-only planning route matrix requested: technicians=%s tickets=%s unique_locations=%s",
        len(technicians),
        len(tickets),
        len(location_ids),
    )
    print(
        "[planning-ai-debug] cached_route_matrix_requested "
        f"technicians={len(technicians)} tickets={len(tickets)} "
        f"unique_locations={len(location_ids)} location_ids={location_ids}",
        flush=True,
    )
    if len(location_ids) < 2:
        return RouteMatrix(travel_minutes={}, distance_km={})

    rows = session.scalars(
        select(RouteCache).where(
            RouteCache.provider == RouteProvider.OSRM,
            RouteCache.from_location_id.in_(location_ids),
            RouteCache.to_location_id.in_(location_ids),
        )
    ).all()
    print(
        "[planning-ai-debug] cached_route_matrix_rows_loaded "
        f"row_count={len(rows)} expected_directed_pairs={max(0, len(location_ids) * (len(location_ids) - 1))}",
        flush=True,
    )
    cached = {
        (row.from_location_id, row.to_location_id): row
        for row in rows
    }

    travel_minutes: dict[tuple[int, int], int] = {}
    distance_km: dict[tuple[int, int], float] = {}
    missing_pairs: list[tuple[int, int]] = []
    for from_id in location_ids:
        for to_id in location_ids:
            if from_id == to_id:
                travel_minutes[(from_id, to_id)] = 0
                distance_km[(from_id, to_id)] = 0.0
                continue
            leg = cached.get((from_id, to_id))
            if leg is None:
                missing_pairs.append((from_id, to_id))
                continue
            travel_minutes[(from_id, to_id)] = int(leg.travel_minutes)
            distance_km[(from_id, to_id)] = float(leg.distance_km)

    if missing_pairs:
        sample = ", ".join(f"{a}->{b}" for a, b in missing_pairs[:10])
        suffix = "..." if len(missing_pairs) > 10 else ""
        print(
            "[planning-ai-debug] cached_route_matrix_missing_pairs "
            f"missing_count={len(missing_pairs)} sample={sample}{suffix}",
            flush=True,
        )
        raise PlanningRoutingError(
            "Operational daytime replanning uses the database routing matrix only; "
            f"{len(missing_pairs)} directed route pair(s) are missing from route_cache: {sample}{suffix}."
        )

    print(
        "[planning-ai-debug] cached_route_matrix_finished "
        f"travel_pairs={len(travel_minutes)} distance_pairs={len(distance_km)}",
        flush=True,
    )
    return RouteMatrix(travel_minutes=travel_minutes, distance_km=distance_km)

def get_incremental_planning_route_matrix(
    session: Session,
    technicians: list[TechnicianInput],
    existing_tickets: list[TicketInput],
    new_ticket: TicketInput,
    *,
    refresh_cache: bool = False,
) -> RouteMatrix:
    """Build a planner matrix for one incoming ticket without a full OSRM table.

    Existing assignment-to-assignment legs should already be cached by the last
    completed planning run. For incremental replanning we only fetch missing
    directed legs that either start at or end at the incoming ticket location.
    This keeps the OSRM request small: one row and one column around the new
    ticket instead of an all-locations x all-locations table.
    """
    all_tickets = [*existing_tickets, new_ticket]
    location_ids = sorted(
        {
            *[technician.start_location_id for technician in technicians],
            *[technician.end_location_id for technician in technicians],
            *[technician.office_location_id for technician in technicians],
            *[ticket.location_id for ticket in all_tickets],
        }
    )
    logger.debug(
        "Incremental route matrix requested: technicians=%s existing_tickets=%s new_ticket_id=%s unique_locations=%s refresh_cache=%s",
        len(technicians),
        len(existing_tickets),
        new_ticket.id,
        len(location_ids),
        refresh_cache,
    )
    if len(location_ids) < 2:
        logger.debug("Incremental route matrix has fewer than 2 locations; returning empty matrix")
        return RouteMatrix(travel_minutes={}, distance_km={})

    locations = list(session.query(Location).filter(Location.id.in_(location_ids)).all())
    by_id = {location.id: location for location in locations}
    missing = [location_id for location_id in location_ids if location_id not in by_id]
    if missing:
        raise PlanningRoutingError(f"Missing route locations: {missing}")

    point_by_id: dict[int, RoutePoint] = {}
    for location_id in location_ids:
        try:
            point_by_id[location_id] = RoutePoint.from_location(by_id[location_id])
        except ValueError as exc:
            raise PlanningRoutingError(f"Location {location_id} has no usable coordinates") from exc

    new_location_id = new_ticket.location_id
    other_location_ids = [location_id for location_id in location_ids if location_id != new_location_id]
    logger.debug(
        "Incremental route matrix will request only new ticket row/column: new_ticket_id=%s new_location_id=%s other_locations=%s",
        new_ticket.id,
        new_location_id,
        len(other_location_ids),
    )

    try:
        # Fetch only the new-ticket row and column. Existing-existing legs are
        # read from cache below and are never re-requested as a full matrix here.
        logger.debug(
            "Incremental route matrix fetching/loading legs FROM new location %s to %s existing location(s)",
            new_location_id,
            len(other_location_ids),
        )
        legs_from_new = routing_service._get_or_fetch_legs_between(  # noqa: SLF001
            session,
            [point_by_id[new_location_id]],
            [point_by_id[location_id] for location_id in other_location_ids],
            refresh_cache=refresh_cache,
        )
        logger.debug("Incremental route matrix loaded %s leg(s) FROM new location", len(legs_from_new))
        logger.debug(
            "Incremental route matrix fetching/loading legs TO new location %s from %s existing location(s)",
            new_location_id,
            len(other_location_ids),
        )
        legs_to_new = routing_service._get_or_fetch_legs_between(  # noqa: SLF001
            session,
            [point_by_id[location_id] for location_id in other_location_ids],
            [point_by_id[new_location_id]],
            refresh_cache=refresh_cache,
        )
        logger.debug("Incremental route matrix loaded %s leg(s) TO new location", len(legs_to_new))
        cached_existing = routing_service._load_cached_legs_between(  # noqa: SLF001
            session,
            other_location_ids,
            other_location_ids,
        )
        logger.debug(
            "Incremental route matrix loaded %s cached existing-existing leg(s); no full OSRM matrix requested here",
            len(cached_existing),
        )
    except Exception as exc:
        raise PlanningRoutingError(str(exc)) from exc

    legs = {}
    legs.update(cached_existing)
    legs.update(legs_from_new)
    legs.update(legs_to_new)

    travel_minutes: dict[tuple[int, int], int] = {}
    distance_km: dict[tuple[int, int], float] = {}
    missing_cached_pairs: list[tuple[int, int]] = []
    for from_id in location_ids:
        for to_id in location_ids:
            if from_id == to_id:
                travel_minutes[(from_id, to_id)] = 0
                distance_km[(from_id, to_id)] = 0.0
                continue
            leg = legs.get((from_id, to_id))
            if leg is None:
                missing_cached_pairs.append((from_id, to_id))
                continue
            travel_minutes[(from_id, to_id)] = leg.travel_minutes
            distance_km[(from_id, to_id)] = leg.distance_km

    if missing_cached_pairs:
        logger.debug(
            "Incremental route matrix missing %s cached existing-existing pair(s)",
            len(missing_cached_pairs),
        )
        sample = ", ".join(f"{a}->{b}" for a, b in missing_cached_pairs[:8])
        suffix = "..." if len(missing_cached_pairs) > 8 else ""
        raise PlanningRoutingError(
            "Incremental planning needs cached legs from the active plan, but "
            f"{len(missing_cached_pairs)} existing route pair(s) were missing: {sample}{suffix}. "
            "Run the normal planner once to warm the cache, then the incremental worker "
            "will only fetch the incoming-ticket row/column."
        )

    logger.debug(
        "Incremental route matrix complete: total_directed_pairs=%s from_new=%s to_new=%s cached_existing=%s",
        len(travel_minutes),
        len(legs_from_new),
        len(legs_to_new),
        len(cached_existing),
    )
    return RouteMatrix(travel_minutes=travel_minutes, distance_km=distance_km)


def _get_or_fetch_complete_matrix(
    session: Session,
    points: list[RoutePoint],
    *,
    refresh_cache: bool,
) -> dict[tuple[int, int], object]:
    """Fetch/cache every directed pair, chunked around the OSRM 100-point limit.

    Initial planning intentionally warms the route cache for all known tickets.
    By batching source and destination groups we can still populate the complete
    matrix when the branch has more than 100 unique locations.
    """
    if len(points) <= 100:
        logger.debug("Complete route matrix using single OSRM/cache request for %s point(s)", len(points))
        return routing_service._get_or_fetch_legs(session, points, refresh_cache=refresh_cache)  # noqa: SLF001

    legs: dict[tuple[int, int], object] = {}
    batch_size = 50
    logger.debug(
        "Complete route matrix using chunked source/destination requests for %s point(s) with batch_size=%s",
        len(points),
        batch_size,
    )
    for source_start in range(0, len(points), batch_size):
        source_batch = points[source_start : source_start + batch_size]
        for destination_start in range(0, len(points), batch_size):
            destination_batch = points[destination_start : destination_start + batch_size]
            logger.debug(
                "Complete route matrix fetching/loading chunk: sources=%s destinations=%s",
                len(source_batch),
                len(destination_batch),
            )
            legs.update(
                routing_service._get_or_fetch_legs_between(  # noqa: SLF001
                    session,
                    source_batch,
                    destination_batch,
                    refresh_cache=refresh_cache,
                )
            )
    return legs
