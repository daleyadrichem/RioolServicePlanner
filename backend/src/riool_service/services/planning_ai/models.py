from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from riool_service.database.models.tickets import TicketUrgency


@dataclass(frozen=True)
class PlanningConfig:
    """Configuration for the initial planning optimizer.

    The defaults are intentionally demo-friendly: the optimizer creates multiple
    different starting plans, improves each one, and returns the best result.
    """

    branch_id: int
    planned_date: datetime
    max_candidates_per_technician: int = 10
    initial_non_urgent_minutes_per_technician: int = 6 * 60
    default_service_minutes: int = 60
    multi_start_iterations: int = 40
    local_search_iterations: int = 250
    random_seed: int | None = 42
    refresh_route_cache: bool = False
    low_priority_max_extra_travel_minutes: int = 35


@dataclass(frozen=True)
class TechnicianInput:
    id: int
    name: str
    start_location_id: int
    end_location_id: int
    workday_start_minutes: int
    workday_end_minutes: int
    requirement_codes: frozenset[str]


@dataclass(frozen=True)
class TicketInput:
    id: int
    location_id: int
    urgency: TicketUrgency
    deadline_at: datetime
    created_at: datetime
    service_minutes: int
    requirement_codes: frozenset[str]
    subject: str | None = None
    address: str = ""

    @property
    def is_low_priority(self) -> bool:
        return self.urgency == TicketUrgency.LOW

    @property
    def urgency_rank(self) -> int:
        return {
            TicketUrgency.URGENT: 0,
            TicketUrgency.MEDIUM: 1,
            TicketUrgency.LOW: 2,
        }[self.urgency]


@dataclass
class PlannedStop:
    ticket: TicketInput
    travel_minutes_before: int
    distance_km_before: float
    planned_start_at: datetime
    planned_end_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket.id,
            "location_id": self.ticket.location_id,
            "urgency": self.ticket.urgency.value,
            "subject": self.ticket.subject,
            "address": self.ticket.address,
            "required_skills": sorted(self.ticket.requirement_codes),
            "travel_minutes_before": self.travel_minutes_before,
            "distance_km_before": round(self.distance_km_before, 3),
            "planned_start_at": self.planned_start_at.isoformat(),
            "planned_end_at": self.planned_end_at.isoformat(),
            "deadline_at": self.ticket.deadline_at.isoformat(),
            "estimated_duration_minutes": self.ticket.service_minutes,
        }


@dataclass
class MechanicRoute:
    technician: TechnicianInput
    ticket_ids: list[int] = field(default_factory=list)

    def copy(self) -> "MechanicRoute":
        return MechanicRoute(technician=self.technician, ticket_ids=self.ticket_ids[:])


@dataclass
class PlanningSolution:
    routes: dict[int, MechanicRoute]
    unplanned_ticket_ids: set[int]
    score: float = float("inf")
    total_travel_minutes: int = 0
    total_distance_km: float = 0.0
    completed_tickets: int = 0
    sla_misses: int = 0
    overtime_minutes: int = 0
    algorithm_notes: list[str] = field(default_factory=list)

    def copy(self) -> "PlanningSolution":
        return PlanningSolution(
            routes={technician_id: route.copy() for technician_id, route in self.routes.items()},
            unplanned_ticket_ids=set(self.unplanned_ticket_ids),
            score=self.score,
            total_travel_minutes=self.total_travel_minutes,
            total_distance_km=self.total_distance_km,
            completed_tickets=self.completed_tickets,
            sla_misses=self.sla_misses,
            overtime_minutes=self.overtime_minutes,
            algorithm_notes=self.algorithm_notes[:],
        )


@dataclass(frozen=True)
class RouteMatrix:
    travel_minutes: dict[tuple[int, int], int]
    distance_km: dict[tuple[int, int], float]

    def duration(self, from_location_id: int, to_location_id: int) -> int:
        if from_location_id == to_location_id:
            return 0
        return self.travel_minutes[(from_location_id, to_location_id)]

    def distance(self, from_location_id: int, to_location_id: int) -> float:
        if from_location_id == to_location_id:
            return 0.0
        return self.distance_km[(from_location_id, to_location_id)]
