from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from riool_service.database.models.location import Location
from riool_service.database.models.tickets import Ticket


@dataclass(frozen=True)
class RoutePoint:
    """A routable point used by routing providers.

    OSRM expects coordinates as longitude,latitude. The database stores both
    fields separately, so this small DTO keeps provider code independent from
    SQLAlchemy models.
    """

    id: int
    latitude: float
    longitude: float
    label: str = ""

    @classmethod
    def from_location(cls, location: Location, label: str | None = None) -> "RoutePoint":
        if location.latitude is None or location.longitude is None:
            raise ValueError(f"Location {location.id} has no latitude/longitude")
        return cls(
            id=location.id,
            latitude=float(location.latitude),
            longitude=float(location.longitude),
            label=label or location.formatted_address or location.input_address or str(location.id),
        )

    def osrm_coordinate(self) -> str:
        return f"{self.longitude:.7f},{self.latitude:.7f}"


@dataclass(frozen=True)
class RouteLeg:
    from_location_id: int
    to_location_id: int
    travel_minutes: int
    distance_km: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_location_id": self.from_location_id,
            "to_location_id": self.to_location_id,
            "travel_minutes": self.travel_minutes,
            "distance_km": round(self.distance_km, 3),
        }


@dataclass(frozen=True)
class TicketRouteStop:
    ticket_id: int
    location_id: int
    address: str
    urgency: str
    status: str
    subject: str | None = None

    @classmethod
    def from_ticket(cls, ticket: Ticket) -> "TicketRouteStop":
        location = ticket.location
        address = ""
        if location is not None:
            address = location.formatted_address or location.input_address or ""
        return cls(
            ticket_id=ticket.id,
            location_id=ticket.location_id,
            address=address,
            urgency=getattr(ticket.urgency, "value", str(ticket.urgency)),
            status=getattr(ticket.status, "value", str(ticket.status)),
            subject=ticket.subject.name if ticket.subject is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "location_id": self.location_id,
            "address": self.address,
            "urgency": self.urgency,
            "status": self.status,
            "subject": self.subject,
        }
