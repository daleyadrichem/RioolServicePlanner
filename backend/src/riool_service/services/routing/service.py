from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, joinedload

from riool_service.database.db_utils import get_engine
from riool_service.database.models.base import Base
from riool_service.database.models.location import Location
from riool_service.database.models.route_cache import RouteCache, RouteProvider
from riool_service.database.models.tickets import Ticket
from riool_service.services.routing.models import RouteLeg, RoutePoint, TicketRouteStop
from riool_service.services.routing.providers.osrm_provider import OsrmProvider, OsrmProviderError

CACHE_TTL_DAYS = 30
MAX_OPTIMIZATION_TICKETS = 25

logger = logging.getLogger(__name__)


class RoutingError(ValueError):
    """Raised when a routing request cannot be completed."""


class TicketRoutingNotFoundError(RoutingError):
    """Raised when requested tickets do not exist."""


def ensure_routing_tables() -> None:
    """Create routing tables that may not exist yet in older local databases."""
    Base.metadata.create_all(get_engine())


def get_ticket_route_matrix(
    session: Session,
    ticket_ids: list[int],
    *,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    """Return a duration/distance matrix for the selected ticket locations."""
    tickets = _load_tickets(session, ticket_ids)
    points = _points_from_tickets(tickets)
    legs = _get_or_fetch_legs(session, points, refresh_cache=refresh_cache)

    matrix = []
    for from_ticket in tickets:
        row = []
        for to_ticket in tickets:
            if from_ticket.id == to_ticket.id:
                row.append(
                    {
                        "from_ticket_id": from_ticket.id,
                        "to_ticket_id": to_ticket.id,
                        "travel_minutes": 0,
                        "distance_km": 0.0,
                    }
                )
                continue
            leg = legs.get((from_ticket.location_id, to_ticket.location_id))
            row.append(
                {
                    "from_ticket_id": from_ticket.id,
                    "to_ticket_id": to_ticket.id,
                    "travel_minutes": leg.travel_minutes if leg else None,
                    "distance_km": round(leg.distance_km, 3) if leg else None,
                }
            )
        matrix.append(row)

    return {
        "ticket_ids": [ticket.id for ticket in tickets],
        "stops": [_ticket_stop(ticket) for ticket in tickets],
        "matrix": matrix,
    }


def get_ticket_route_matrix_between(
    session: Session,
    source_ticket_ids: list[int],
    destination_ticket_ids: list[int],
    *,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    """Return a duration/distance matrix from selected sources to selected destinations.

    Unlike ``get_ticket_route_matrix``, this only fetches/calculates the requested
    source -> destination pairs. This is useful for planner steps where, for
    example, only the current mechanic positions to candidate tickets are needed.
    """
    source_tickets = _load_tickets(session, source_ticket_ids, minimum_count=1, field_name="source_ticket_ids")
    destination_tickets = _load_tickets(
        session,
        destination_ticket_ids,
        minimum_count=1,
        field_name="destination_ticket_ids",
    )
    source_points = _points_from_tickets(source_tickets)
    destination_points = _points_from_tickets(destination_tickets)
    legs = _get_or_fetch_legs_between(
        session,
        source_points,
        destination_points,
        refresh_cache=refresh_cache,
    )

    matrix = []
    for source_ticket in source_tickets:
        row = []
        for destination_ticket in destination_tickets:
            if source_ticket.location_id == destination_ticket.location_id:
                row.append(
                    {
                        "from_ticket_id": source_ticket.id,
                        "to_ticket_id": destination_ticket.id,
                        "travel_minutes": 0,
                        "distance_km": 0.0,
                    }
                )
                continue
            leg = legs.get((source_ticket.location_id, destination_ticket.location_id))
            row.append(
                {
                    "from_ticket_id": source_ticket.id,
                    "to_ticket_id": destination_ticket.id,
                    "travel_minutes": leg.travel_minutes if leg else None,
                    "distance_km": round(leg.distance_km, 3) if leg else None,
                }
            )
        matrix.append(row)

    return {
        "source_ticket_ids": [ticket.id for ticket in source_tickets],
        "destination_ticket_ids": [ticket.id for ticket in destination_tickets],
        "source_stops": [_ticket_stop(ticket) for ticket in source_tickets],
        "destination_stops": [_ticket_stop(ticket) for ticket in destination_tickets],
        "matrix": matrix,
    }


def optimize_ticket_order(
    session: Session,
    ticket_ids: list[int],
    *,
    start_ticket_id: int | None = None,
    end_ticket_id: int | None = None,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    """Return a quick route order for a selection of tickets.

    This uses OSRM's table endpoint once, then solves the local ordering problem
    with a nearest-neighbour seed and 2-opt improvement. That is fast enough for
    planner interactions and avoids many separate OSRM calls.
    """
    tickets = _load_tickets(session, ticket_ids)
    if len(tickets) > MAX_OPTIMIZATION_TICKETS:
        raise RoutingError(
            f"Route optimization currently accepts at most {MAX_OPTIMIZATION_TICKETS} tickets; got {len(tickets)}"
        )
    ticket_by_id = {ticket.id: ticket for ticket in tickets}
    if start_ticket_id is not None and start_ticket_id not in ticket_by_id:
        raise RoutingError("start_ticket_id must be one of the selected ticket_ids")
    if end_ticket_id is not None and end_ticket_id not in ticket_by_id:
        raise RoutingError("end_ticket_id must be one of the selected ticket_ids")
    if start_ticket_id is not None and end_ticket_id is not None and start_ticket_id == end_ticket_id and len(tickets) > 1:
        raise RoutingError("start_ticket_id and end_ticket_id cannot be equal for multiple tickets")

    points = _points_from_tickets(tickets)
    legs_by_location = _get_or_fetch_legs(session, points, refresh_cache=refresh_cache)
    duration_by_ticket = _duration_lookup(tickets, legs_by_location)

    order = _nearest_neighbour_order(
        [ticket.id for ticket in tickets],
        duration_by_ticket,
        start_ticket_id=start_ticket_id,
        end_ticket_id=end_ticket_id,
    )
    order = _two_opt(order, duration_by_ticket, fixed_start=start_ticket_id is not None, fixed_end=end_ticket_id is not None)

    ordered_tickets = [ticket_by_id[ticket_id] for ticket_id in order]
    legs = []
    total_minutes = 0
    total_km = 0.0
    for current_ticket, next_ticket in zip(ordered_tickets, ordered_tickets[1:]):
        leg = legs_by_location.get((current_ticket.location_id, next_ticket.location_id))
        if leg is None:
            raise RoutingError(f"No OSRM route found between ticket {current_ticket.id} and {next_ticket.id}")
        total_minutes += leg.travel_minutes
        total_km += leg.distance_km
        legs.append(
            {
                "from_ticket_id": current_ticket.id,
                "to_ticket_id": next_ticket.id,
                **leg.as_dict(),
            }
        )

    return {
        "ticket_ids": [ticket.id for ticket in tickets],
        "ordered_ticket_ids": order,
        "total_travel_minutes": total_minutes,
        "total_distance_km": round(total_km, 3),
        "stops": [_ticket_stop(ticket) for ticket in ordered_tickets],
        "legs": legs,
        "algorithm": "nearest_neighbour_plus_2opt",
    }


def _load_tickets(
    session: Session,
    ticket_ids: list[int],
    *,
    minimum_count: int = 2,
    field_name: str = "ticket_ids",
) -> list[Ticket]:
    cleaned_ids = _clean_ids(ticket_ids, field_name=field_name)
    if len(cleaned_ids) < minimum_count:
        raise RoutingError(f"At least {minimum_count} {field_name} are required for routing")

    result = session.execute(
        select(Ticket)
        .options(joinedload(Ticket.location), joinedload(Ticket.subject))
        .where(Ticket.id.in_(cleaned_ids))
    )
    tickets = list(result.unique().scalars())
    found_ids = {ticket.id for ticket in tickets}
    missing_ids = [ticket_id for ticket_id in cleaned_ids if ticket_id not in found_ids]
    if missing_ids:
        raise TicketRoutingNotFoundError(f"Unknown {field_name}: {missing_ids}")

    ticket_by_id = {ticket.id: ticket for ticket in tickets}
    return [ticket_by_id[ticket_id] for ticket_id in cleaned_ids]


def _clean_ids(ticket_ids: list[int], *, field_name: str = "ticket_ids") -> list[int]:
    seen: set[int] = set()
    cleaned: list[int] = []
    for raw_id in ticket_ids:
        ticket_id = int(raw_id)
        if ticket_id <= 0:
            raise RoutingError(f"{field_name} must be positive integers")
        if ticket_id not in seen:
            cleaned.append(ticket_id)
            seen.add(ticket_id)
    return cleaned


def _points_from_tickets(tickets: list[Ticket]) -> list[RoutePoint]:
    points: list[RoutePoint] = []
    for ticket in tickets:
        if ticket.location is None:
            raise RoutingError(f"Ticket {ticket.id} has no location")
        try:
            points.append(RoutePoint.from_location(ticket.location, label=f"ticket:{ticket.id}"))
        except ValueError as exc:
            raise RoutingError(f"Ticket {ticket.id} location has no coordinates") from exc
    return points


def _get_or_fetch_legs(
    session: Session,
    points: list[RoutePoint],
    *,
    refresh_cache: bool,
) -> dict[tuple[int, int], RouteLeg]:
    location_ids = [point.id for point in points]
    cached = {} if refresh_cache else _load_cached_legs(session, location_ids)
    required_pairs = {(a, b) for a in location_ids for b in location_ids if a != b}
    missing_pairs = required_pairs - set(cached)
    logger.debug(
        "Routing full-matrix request: points=%s required_pairs=%s cached=%s missing=%s refresh_cache=%s",
        len(points),
        len(required_pairs),
        len(cached),
        len(missing_pairs),
        refresh_cache,
    )

    if not missing_pairs:
        logger.debug("Routing full-matrix request served entirely from cache")
        return cached

    provider = OsrmProvider.from_env()
    logger.debug("Routing full-matrix request calling OSRM table for %s point(s)", len(points))
    fetched = provider.table(points)
    if not fetched:
        raise RoutingError("OSRM did not return any routes for the selected tickets")

    logger.debug("Routing full-matrix OSRM returned %s leg(s); upserting into cache", len(fetched))
    _upsert_cache(session, fetched)
    cached.update(fetched)
    return cached


def _get_or_fetch_legs_between(
    session: Session,
    source_points: list[RoutePoint],
    destination_points: list[RoutePoint],
    *,
    refresh_cache: bool,
) -> dict[tuple[int, int], RouteLeg]:
    source_location_ids = [point.id for point in source_points]
    destination_location_ids = [point.id for point in destination_points]
    cached = (
        {}
        if refresh_cache
        else _load_cached_legs_between(session, source_location_ids, destination_location_ids)
    )
    required_pairs = {
        (source_id, destination_id)
        for source_id in source_location_ids
        for destination_id in destination_location_ids
        if source_id != destination_id
    }
    missing_pairs = required_pairs - set(cached)
    logger.debug(
        "Routing between request: sources=%s destinations=%s required_pairs=%s cached=%s missing=%s refresh_cache=%s",
        len(source_points),
        len(destination_points),
        len(required_pairs),
        len(cached),
        len(missing_pairs),
        refresh_cache,
    )

    if not missing_pairs:
        logger.debug("Routing between request served entirely from cache")
        return cached

    missing_source_ids = {source_id for source_id, _ in missing_pairs}
    missing_destination_ids = {destination_id for _, destination_id in missing_pairs}
    provider = OsrmProvider.from_env()
    logger.debug(
        "Routing between request calling OSRM table_between for missing sources=%s destinations=%s",
        len(missing_source_ids),
        len(missing_destination_ids),
    )
    fetched = provider.table_between(
        [point for point in source_points if point.id in missing_source_ids],
        [point for point in destination_points if point.id in missing_destination_ids],
    )
    if not fetched:
        raise RoutingError("OSRM did not return any routes for the selected source/destination tickets")

    relevant_fetched = {pair: leg for pair, leg in fetched.items() if pair in missing_pairs}
    logger.debug(
        "Routing between OSRM returned %s leg(s), %s relevant missing leg(s); upserting into cache",
        len(fetched),
        len(relevant_fetched),
    )
    _upsert_cache(session, relevant_fetched)
    cached.update(relevant_fetched)
    return cached


def _load_cached_legs(session: Session, location_ids: list[int]) -> dict[tuple[int, int], RouteLeg]:
    now = datetime.utcnow()
    logger.debug("Routing cache load full: locations=%s", len(location_ids))
    rows = session.execute(
        select(RouteCache).where(
            and_(
                RouteCache.provider == RouteProvider.OSRM,
                RouteCache.from_location_id.in_(location_ids),
                RouteCache.to_location_id.in_(location_ids),
            )
        )
    ).scalars()

    result: dict[tuple[int, int], RouteLeg] = {}
    for row in rows:
        if row.expires_at is not None and row.expires_at < now:
            continue
        result[(row.from_location_id, row.to_location_id)] = RouteLeg(
            from_location_id=row.from_location_id,
            to_location_id=row.to_location_id,
            travel_minutes=row.travel_minutes,
            distance_km=row.distance_km,
        )
    logger.debug("Routing cache load full returned %s non-expired leg(s)", len(result))
    return result


def _load_cached_legs_between(
    session: Session,
    source_location_ids: list[int],
    destination_location_ids: list[int],
) -> dict[tuple[int, int], RouteLeg]:
    now = datetime.utcnow()
    logger.debug(
        "Routing cache load between: sources=%s destinations=%s",
        len(source_location_ids),
        len(destination_location_ids),
    )
    rows = session.execute(
        select(RouteCache).where(
            and_(
                RouteCache.provider == RouteProvider.OSRM,
                RouteCache.from_location_id.in_(source_location_ids),
                RouteCache.to_location_id.in_(destination_location_ids),
            )
        )
    ).scalars()

    result: dict[tuple[int, int], RouteLeg] = {}
    for row in rows:
        if row.expires_at is not None and row.expires_at < now:
            continue
        result[(row.from_location_id, row.to_location_id)] = RouteLeg(
            from_location_id=row.from_location_id,
            to_location_id=row.to_location_id,
            travel_minutes=row.travel_minutes,
            distance_km=row.distance_km,
        )
    logger.debug("Routing cache load between returned %s non-expired leg(s)", len(result))
    return result


def _upsert_cache(session: Session, legs: dict[tuple[int, int], RouteLeg]) -> None:
    expires_at = datetime.utcnow() + timedelta(days=CACHE_TTL_DAYS)
    for (from_location_id, to_location_id), leg in legs.items():
        existing = session.execute(
            select(RouteCache).where(
                and_(
                    RouteCache.from_location_id == from_location_id,
                    RouteCache.to_location_id == to_location_id,
                    RouteCache.provider == RouteProvider.OSRM,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                RouteCache(
                    from_location_id=from_location_id,
                    to_location_id=to_location_id,
                    provider=RouteProvider.OSRM,
                    travel_minutes=leg.travel_minutes,
                    distance_km=leg.distance_km,
                    calculated_at=datetime.utcnow(),
                    expires_at=expires_at,
                )
            )
        else:
            existing.travel_minutes = leg.travel_minutes
            existing.distance_km = leg.distance_km
            existing.calculated_at = datetime.utcnow()
            existing.expires_at = expires_at


def _duration_lookup(
    tickets: list[Ticket],
    legs_by_location: dict[tuple[int, int], RouteLeg],
) -> dict[tuple[int, int], int]:
    lookup: dict[tuple[int, int], int] = {}
    for from_ticket in tickets:
        for to_ticket in tickets:
            if from_ticket.id == to_ticket.id:
                lookup[(from_ticket.id, to_ticket.id)] = 0
                continue
            leg = legs_by_location.get((from_ticket.location_id, to_ticket.location_id))
            if leg is None:
                raise RoutingError(f"No OSRM route found between ticket {from_ticket.id} and {to_ticket.id}")
            lookup[(from_ticket.id, to_ticket.id)] = leg.travel_minutes
    return lookup


def _nearest_neighbour_order(
    ticket_ids: list[int],
    durations: dict[tuple[int, int], int],
    *,
    start_ticket_id: int | None,
    end_ticket_id: int | None,
) -> list[int]:
    remaining = set(ticket_ids)
    start = start_ticket_id or min(ticket_ids)
    order = [start]
    remaining.remove(start)
    if end_ticket_id is not None and end_ticket_id in remaining:
        remaining.remove(end_ticket_id)

    while remaining:
        current = order[-1]
        next_ticket = min(remaining, key=lambda ticket_id: durations[(current, ticket_id)])
        order.append(next_ticket)
        remaining.remove(next_ticket)

    if end_ticket_id is not None:
        order.append(end_ticket_id)
    return order


def _two_opt(
    order: list[int],
    durations: dict[tuple[int, int], int],
    *,
    fixed_start: bool,
    fixed_end: bool,
) -> list[int]:
    if len(order) < 4:
        return order

    best = order[:]
    improved = True
    start_index = 1 if fixed_start else 0
    end_limit = len(best) - 1 if fixed_end else len(best)

    while improved:
        improved = False
        for i in range(start_index, end_limit - 2):
            for j in range(i + 2, end_limit):
                if j - i == 1:
                    continue
                candidate = best[:i] + best[i:j][::-1] + best[j:]
                if _order_duration(candidate, durations) < _order_duration(best, durations):
                    best = candidate
                    improved = True
        order = best
    return best


def _order_duration(order: list[int], durations: dict[tuple[int, int], int]) -> int:
    return sum(durations[(a, b)] for a, b in zip(order, order[1:]))


def _ticket_stop(ticket: Ticket) -> dict[str, Any]:
    return TicketRouteStop.from_ticket(ticket).as_dict()
