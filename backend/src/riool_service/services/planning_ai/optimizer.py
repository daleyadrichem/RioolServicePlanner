from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations

from riool_service.database.models.tickets import TicketUrgency
from riool_service.services.planning_ai.models import (
    MechanicRoute,
    PlannedStop,
    PlanningConfig,
    PlanningSolution,
    RouteMatrix,
    TechnicianInput,
    TicketInput,
)
from riool_service.services.planning_ai.selection import planning_day_end, planning_day_start

SLA_MISS_PENALTY = 1_000_000
UNPLANNED_PENALTY_BY_URGENCY = {
    TicketUrgency.URGENT: 750_000,
    TicketUrgency.MEDIUM: 250_000,
    TicketUrgency.LOW: 20_000,
}
OVERTIME_PENALTY_PER_MINUTE = 500
TRAVEL_PENALTY_PER_MINUTE = 1


@dataclass(frozen=True)
class Insertion:
    technician_id: int
    position: int
    extra_travel_minutes: int
    extra_distance_km: float
    score_delta: float


class InitialRouteOptimizer:
    """Multi-start randomized cheapest insertion + local-search optimizer."""

    def __init__(
        self,
        *,
        config: PlanningConfig,
        technicians: list[TechnicianInput],
        tickets: list[TicketInput],
        matrix: RouteMatrix,
    ) -> None:
        self.config = config
        self.technicians = technicians
        self.tickets = tickets
        self.ticket_by_id = {ticket.id: ticket for ticket in tickets}
        self.matrix = matrix
        self.random = random.Random(config.random_seed)

    def optimize(self) -> PlanningSolution:
        if not self.technicians:
            raise ValueError("At least one technician is required")
        if not self.tickets:
            return self._empty_solution()

        best: PlanningSolution | None = None
        iterations = max(1, self.config.multi_start_iterations)
        for iteration in range(iterations):
            solution = self._build_initial_solution(iteration=iteration)
            solution = self._improve(solution)
            self._score(solution)
            if best is None or solution.score < best.score:
                best = solution

        assert best is not None
        best.algorithm_notes = [
            "Multi-start randomized cheapest insertion",
            "Local search with move, swap and 2-opt reorder operators",
            "Low-priority tickets are only inserted when feasible and route-fit is good",
        ]
        return best

    def build_stops(self, solution: PlanningSolution, technician_id: int) -> list[PlannedStop]:
        route = solution.routes[technician_id]
        technician = route.technician
        current_time = planning_day_start(self.config, technician)
        previous_location_id = technician.start_location_id
        stops: list[PlannedStop] = []
        for ticket_id in route.ticket_ids:
            ticket = self.ticket_by_id[ticket_id]
            travel_minutes = self.matrix.duration(previous_location_id, ticket.location_id)
            distance_km = self.matrix.distance(previous_location_id, ticket.location_id)
            start_at = current_time + timedelta(minutes=travel_minutes)
            end_at = start_at + timedelta(minutes=ticket.service_minutes)
            stops.append(
                PlannedStop(
                    ticket=ticket,
                    travel_minutes_before=travel_minutes,
                    distance_km_before=distance_km,
                    planned_start_at=start_at,
                    planned_end_at=end_at,
                )
            )
            current_time = end_at
            previous_location_id = ticket.location_id
        return stops

    def _build_initial_solution(self, *, iteration: int) -> PlanningSolution:
        solution = self._empty_solution()
        remaining = self._ordered_tickets_for_start(iteration)

        self._seed_routes(solution, remaining, iteration=iteration)

        while remaining:
            ticket = remaining.pop(0)
            insertion = self._best_insertion(solution, ticket, allow_low_priority=True)
            if insertion is None:
                solution.unplanned_ticket_ids.add(ticket.id)
                continue
            solution.routes[insertion.technician_id].ticket_ids.insert(insertion.position, ticket.id)

        self._score(solution)
        return solution

    def _seed_routes(self, solution: PlanningSolution, remaining: list[TicketInput], *, iteration: int) -> None:
        """Create diverse starts so nearby-home mechanics do not all stay local.

        Each route gets a semi-random first ticket from a small feasible candidate
        set. For some starts we deliberately include farther tickets/clusters.
        """
        if iteration == 0:
            return

        for technician in self.technicians:
            feasible = [ticket for ticket in remaining if self._can_do(technician, ticket)]
            if not feasible:
                continue

            feasible.sort(
                key=lambda ticket: (
                    ticket.urgency_rank,
                    ticket.deadline_at,
                    self.matrix.duration(technician.start_location_id, ticket.location_id),
                )
            )
            # Most starts choose from sensible close/deadline tickets. Some starts
            # choose from farther tickets to let one mechanic claim a remote area.
            if iteration % 4 == 0:
                pool = sorted(
                    feasible[: min(20, len(feasible))],
                    key=lambda ticket: self.matrix.duration(technician.start_location_id, ticket.location_id),
                    reverse=True,
                )[: min(5, len(feasible))]
            else:
                pool = feasible[: min(6, len(feasible))]

            seed_ticket = self.random.choice(pool)
            insertion = self._best_insertion(solution, seed_ticket, technician_ids=[technician.id], allow_low_priority=True)
            if insertion is not None:
                solution.routes[technician.id].ticket_ids.insert(insertion.position, seed_ticket.id)
                remaining.remove(seed_ticket)

    def _ordered_tickets_for_start(self, iteration: int) -> list[TicketInput]:
        tickets = self.tickets[:]
        if iteration == 0:
            return sorted(tickets, key=self._strict_priority_key)

        urgent_medium = [ticket for ticket in tickets if ticket.urgency != TicketUrgency.LOW]
        low = [ticket for ticket in tickets if ticket.urgency == TicketUrgency.LOW]
        urgent_medium.sort(key=self._strict_priority_key)
        low.sort(key=self._strict_priority_key)

        # Shuffle within priority bands. This gives different starting points
        # without ignoring SLA pressure.
        urgent_chunks = self._chunked(urgent_medium, size=4)
        low_chunks = self._chunked(low, size=6)
        randomized: list[TicketInput] = []
        for chunk in urgent_chunks:
            self.random.shuffle(chunk)
            randomized.extend(chunk)
        for chunk in low_chunks:
            self.random.shuffle(chunk)
            randomized.extend(chunk)
        return randomized

    def _improve(self, solution: PlanningSolution) -> PlanningSolution:
        best = solution.copy()
        self._score(best)
        for _ in range(max(0, self.config.local_search_iterations)):
            changed = False
            for candidate in self._move_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    best = candidate
                    changed = True
                    break
            if changed:
                continue

            for candidate in self._swap_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    best = candidate
                    changed = True
                    break
            if changed:
                continue

            for candidate in self._two_opt_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    best = candidate
                    changed = True
                    break
            if not changed:
                break
        return best

    def _move_candidates(self, solution: PlanningSolution):
        route_ids = list(solution.routes)
        self.random.shuffle(route_ids)
        for from_technician_id in route_ids:
            from_route = solution.routes[from_technician_id]
            positions = list(range(len(from_route.ticket_ids)))
            self.random.shuffle(positions)
            for position in positions:
                ticket_id = from_route.ticket_ids[position]
                ticket = self.ticket_by_id[ticket_id]
                for to_technician_id in route_ids:
                    if to_technician_id == from_technician_id:
                        continue
                    to_route = solution.routes[to_technician_id]
                    if not self._can_do(to_route.technician, ticket):
                        continue
                    for insert_position in range(len(to_route.ticket_ids) + 1):
                        candidate = solution.copy()
                        candidate.routes[from_technician_id].ticket_ids.pop(position)
                        adjusted_position = insert_position
                        candidate.routes[to_technician_id].ticket_ids.insert(adjusted_position, ticket_id)
                        if self._is_solution_hard_feasible(candidate):
                            yield candidate

    def _swap_candidates(self, solution: PlanningSolution):
        for first_id, second_id in combinations(list(solution.routes), 2):
            first_route = solution.routes[first_id]
            second_route = solution.routes[second_id]
            first_positions = list(range(len(first_route.ticket_ids)))
            second_positions = list(range(len(second_route.ticket_ids)))
            self.random.shuffle(first_positions)
            self.random.shuffle(second_positions)
            for first_position in first_positions:
                for second_position in second_positions:
                    first_ticket = self.ticket_by_id[first_route.ticket_ids[first_position]]
                    second_ticket = self.ticket_by_id[second_route.ticket_ids[second_position]]
                    if not self._can_do(first_route.technician, second_ticket):
                        continue
                    if not self._can_do(second_route.technician, first_ticket):
                        continue
                    candidate = solution.copy()
                    candidate.routes[first_id].ticket_ids[first_position] = second_ticket.id
                    candidate.routes[second_id].ticket_ids[second_position] = first_ticket.id
                    if self._is_solution_hard_feasible(candidate):
                        yield candidate

    def _two_opt_candidates(self, solution: PlanningSolution):
        route_ids = list(solution.routes)
        self.random.shuffle(route_ids)
        for technician_id in route_ids:
            route = solution.routes[technician_id]
            if len(route.ticket_ids) < 4:
                continue
            for i in range(0, len(route.ticket_ids) - 2):
                for j in range(i + 2, len(route.ticket_ids) + 1):
                    candidate = solution.copy()
                    candidate.routes[technician_id].ticket_ids = (
                        route.ticket_ids[:i]
                        + list(reversed(route.ticket_ids[i:j]))
                        + route.ticket_ids[j:]
                    )
                    if self._is_solution_hard_feasible(candidate):
                        yield candidate

    def _best_insertion(
        self,
        solution: PlanningSolution,
        ticket: TicketInput,
        *,
        technician_ids: list[int] | None = None,
        allow_low_priority: bool,
    ) -> Insertion | None:
        best: Insertion | None = None
        candidate_technician_ids = technician_ids or list(solution.routes)
        for technician_id in candidate_technician_ids:
            route = solution.routes[technician_id]
            if not self._can_do(route.technician, ticket):
                continue
            for position in range(len(route.ticket_ids) + 1):
                insertion = self._evaluate_insertion(solution, route, ticket, position)
                if insertion is None:
                    continue
                if ticket.is_low_priority and not allow_low_priority:
                    continue
                if ticket.is_low_priority and insertion.extra_travel_minutes > self.config.low_priority_max_extra_travel_minutes:
                    # Low work is a filler/route-optimizer, not something that may
                    # ruin a good route or block more urgent work.
                    continue
                if best is None or insertion.score_delta < best.score_delta:
                    best = insertion
        return best

    def _evaluate_insertion(
        self,
        solution: PlanningSolution,
        route: MechanicRoute,
        ticket: TicketInput,
        position: int,
    ) -> Insertion | None:
        previous_location_id = self._location_before(route, position)
        next_location_id = self._location_after(route, position)
        old_direct = self.matrix.duration(previous_location_id, next_location_id)
        new_to_ticket = self.matrix.duration(previous_location_id, ticket.location_id)
        ticket_to_next = self.matrix.duration(ticket.location_id, next_location_id)
        extra_travel = new_to_ticket + ticket_to_next - old_direct
        extra_distance = (
            self.matrix.distance(previous_location_id, ticket.location_id)
            + self.matrix.distance(ticket.location_id, next_location_id)
            - self.matrix.distance(previous_location_id, next_location_id)
        )

        candidate = solution.copy()
        candidate.routes[route.technician.id].ticket_ids.insert(position, ticket.id)
        if not self._is_solution_hard_feasible(candidate):
            return None

        priority_bonus = UNPLANNED_PENALTY_BY_URGENCY[ticket.urgency]
        score_delta = extra_travel + ticket.service_minutes - priority_bonus
        return Insertion(
            technician_id=route.technician.id,
            position=position,
            extra_travel_minutes=extra_travel,
            extra_distance_km=extra_distance,
            score_delta=score_delta,
        )

    def _is_solution_hard_feasible(self, solution: PlanningSolution) -> bool:
        for route in solution.routes.values():
            current_time = planning_day_start(self.config, route.technician)
            previous_location_id = route.technician.start_location_id
            non_urgent_minutes = 0
            for ticket_id in route.ticket_ids:
                ticket = self.ticket_by_id[ticket_id]
                current_time += timedelta(minutes=self.matrix.duration(previous_location_id, ticket.location_id))
                if current_time > ticket.deadline_at:
                    return False
                current_time += timedelta(minutes=ticket.service_minutes)
                if ticket.urgency != TicketUrgency.URGENT:
                    non_urgent_minutes += ticket.service_minutes
                previous_location_id = ticket.location_id
            current_time += timedelta(minutes=self.matrix.duration(previous_location_id, route.technician.end_location_id))
            if current_time > planning_day_end(self.config, route.technician):
                return False
            if non_urgent_minutes > self.config.initial_non_urgent_minutes_per_technician:
                return False
        return True

    def _score(self, solution: PlanningSolution) -> None:
        total_travel = 0
        total_distance = 0.0
        completed = 0
        sla_misses = 0
        overtime = 0
        planned_ticket_ids: set[int] = set()

        for route in solution.routes.values():
            current_time = planning_day_start(self.config, route.technician)
            previous_location_id = route.technician.start_location_id
            for ticket_id in route.ticket_ids:
                ticket = self.ticket_by_id[ticket_id]
                travel = self.matrix.duration(previous_location_id, ticket.location_id)
                total_travel += travel
                total_distance += self.matrix.distance(previous_location_id, ticket.location_id)
                current_time += timedelta(minutes=travel)
                if current_time > ticket.deadline_at:
                    sla_misses += 1
                current_time += timedelta(minutes=ticket.service_minutes)
                completed += 1
                planned_ticket_ids.add(ticket_id)
                previous_location_id = ticket.location_id
            travel_home = self.matrix.duration(previous_location_id, route.technician.end_location_id)
            total_travel += travel_home
            total_distance += self.matrix.distance(previous_location_id, route.technician.end_location_id)
            current_time += timedelta(minutes=travel_home)
            day_end = planning_day_end(self.config, route.technician)
            if current_time > day_end:
                overtime += int((current_time - day_end).total_seconds() // 60)

        solution.unplanned_ticket_ids = {ticket.id for ticket in self.tickets if ticket.id not in planned_ticket_ids}
        unplanned_penalty = sum(
            UNPLANNED_PENALTY_BY_URGENCY[self.ticket_by_id[ticket_id].urgency]
            for ticket_id in solution.unplanned_ticket_ids
        )
        solution.total_travel_minutes = total_travel
        solution.total_distance_km = total_distance
        solution.completed_tickets = completed
        solution.sla_misses = sla_misses
        solution.overtime_minutes = overtime
        solution.score = (
            sla_misses * SLA_MISS_PENALTY
            + unplanned_penalty
            + overtime * OVERTIME_PENALTY_PER_MINUTE
            + total_travel * TRAVEL_PENALTY_PER_MINUTE
        )

    def _empty_solution(self) -> PlanningSolution:
        routes = {
            technician.id: MechanicRoute(technician=technician)
            for technician in self.technicians
        }
        return PlanningSolution(
            routes=routes,
            unplanned_ticket_ids={ticket.id for ticket in self.tickets},
        )

    def _can_do(self, technician: TechnicianInput, ticket: TicketInput) -> bool:
        return ticket.requirement_codes.issubset(technician.requirement_codes)

    def _strict_priority_key(self, ticket: TicketInput) -> tuple[int, datetime, int, datetime, int]:
        return (
            ticket.urgency_rank,
            ticket.deadline_at,
            -len(ticket.requirement_codes),
            ticket.created_at,
            ticket.id,
        )

    def _location_before(self, route: MechanicRoute, position: int) -> int:
        if position == 0:
            return route.technician.start_location_id
        return self.ticket_by_id[route.ticket_ids[position - 1]].location_id

    def _location_after(self, route: MechanicRoute, position: int) -> int:
        if position >= len(route.ticket_ids):
            return route.technician.end_location_id
        return self.ticket_by_id[route.ticket_ids[position]].location_id

    def _chunked(self, items: list[TicketInput], *, size: int) -> list[list[TicketInput]]:
        return [items[index : index + size] for index in range(0, len(items), size)]
