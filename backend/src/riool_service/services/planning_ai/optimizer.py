from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import combinations

from riool_service.database.models.tickets import TicketUrgency
from riool_service.services.planning_ai.models import (
    MechanicRoute,
    PlannedBreak,
    PlannedStop,
    PlannedTravel,
    PlannedRequirementPickup,
    PlanningConfig,
    PlanningSolution,
    RouteMatrix,
    TechnicianInput,
    TicketInput,
)
from riool_service.services.planning_ai.selection import planning_day_end, planning_day_start

SLA_MISS_PENALTY = 100
# A 3-day initial plan should strongly prefer getting every feasible ticket onto
# the board. This base penalty is intentionally urgency-neutral: leaving a
# normal/low ticket unplanned is still bad because it makes the next planning run
# harder. Urgency only adds a small tie-breaker below.
UNPLANNED_TICKET_PENALTY = 1_000_000
UNPLANNED_URGENCY_TIEBREAKER = {
    TicketUrgency.URGENT: 1_500,
    # Medium and low are intentionally almost equal. With the default
    # travel_penalty_per_minute=25, this preference is only worth two minutes
    # of extra travel, so route efficiency can easily outweigh it.
    TicketUrgency.MEDIUM: 50,
    TicketUrgency.LOW: 0,
}
TICKET_COMPLETION_REWARD = UNPLANNED_TICKET_PENALTY
OVERTIME_PENALTY_PER_MINUTE = 500


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
            solution = self._empty_solution()
            self._score(solution)
            return solution

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
            "Medium and low tickets are treated as the same planning class; medium only receives a tiny score tie-breaker.",
            "A 45 minute break is planned for every mechanic inside the 11:00-13:00 window",
            "If a route contains tickets with requirements, a single HQ pickup is inserted before the first required ticket.",
            "Initial route workload is capped using service + travel + HQ pickup time, so urgent-ticket capacity remains free.",
            "Deadline misses are scored softly instead of blocking otherwise efficient medium/low plans.",
            "Every minute of travel, including HQ detours and return-home travel, is penalized equally in the score.",
            "Travel time can outweigh the small medium-over-low tie-breaker.",
        ]
        return best

    def build_stops(self, solution: PlanningSolution, technician_id: int) -> list[PlannedStop]:
        return [
            item
            for item in self.build_timeline(solution, technician_id, include_return_home=False)
            if isinstance(item, PlannedStop)
        ]

    def build_timeline(
        self,
        solution: PlanningSolution,
        technician_id: int,
        *,
        include_return_home: bool = True,
    ) -> list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak]:
        route = solution.routes[technician_id]
        timeline = self._route_timeline(route, include_return_home=include_return_home)
        if timeline is None:
            return []
        return timeline

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
                    self._planning_class_rank(ticket),
                    self.matrix.duration(technician.start_location_id, ticket.location_id),
                    ticket.created_at,
                    ticket.id,
                )
            )
            # Most starts choose from sensible nearby tickets. Some starts choose
            # from farther tickets to let one mechanic claim a remote area.
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

        tickets.sort(key=self._strict_priority_key)

        # Shuffle within broad planning classes. Urgent tickets stay protected,
        # but medium and low share the same class so medium can no longer form a
        # separate band ahead of low work.
        randomized: list[TicketInput] = []
        urgent = [ticket for ticket in tickets if ticket.urgency == TicketUrgency.URGENT]
        normal = [ticket for ticket in tickets if ticket.urgency != TicketUrgency.URGENT]
        for chunk in self._chunked(urgent, size=4):
            self.random.shuffle(chunk)
            randomized.extend(chunk)
        for chunk in self._chunked(normal, size=6):
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
                # Initial planning should assign every feasible open ticket within
                # the configured multi-day horizon. Low-priority work used to be
                # skipped when it added more than ``low_priority_max_extra_travel_minutes``
                # of travel. That made sense for opportunistic same-day filler work,
                # but it left normal/low tickets unplanned even when day 2 or day 3
                # still had capacity. Hard feasibility below still protects
                # workday, skill, lunch-break and daily non-urgent capacity rules.
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
        # Evaluate the real route delta, not just the direct edge replacement.
        # A ticket with requirements may introduce, remove, or move the one-time
        # HQ pickup. The direct calculation below used to ignore that detour,
        # so cheapest-insertion could prefer routes that only looked cheap before
        # the timeline builder inserted home/previous stop -> HQ -> ticket.
        old_travel, old_distance = self._route_travel_and_distance(route)

        candidate = solution.copy()
        candidate_route = candidate.routes[route.technician.id]
        candidate_route.ticket_ids.insert(position, ticket.id)
        if not self._is_solution_hard_feasible(candidate):
            return None

        new_travel, new_distance = self._route_travel_and_distance(candidate_route)
        extra_travel = new_travel - old_travel
        extra_distance = new_distance - old_distance
        old_route_work = self._route_work_minutes(
            self._route_timeline(route, include_return_home=True) or []
        )
        new_route_work = self._route_work_minutes(
            self._route_timeline(candidate_route, include_return_home=True) or []
        )
        extra_route_work_overflow_penalty = (
            self._route_work_overflow_penalty_points(new_route_work)
            - self._route_work_overflow_penalty_points(old_route_work)
        )

        # Insertion should mostly answer: can we place one more feasible ticket
        # without too much extra route work? The completion reward makes it very
        # attractive to get tickets off the board, while the urgency tie-breaker
        # is deliberately small so medium-vs-low priority does not swamp travel.
        travel_penalty = max(0, self.config.travel_penalty_per_minute)
        defer_penalty = max(0, self.config.defer_unplanned_penalty_minutes) * travel_penalty
        priority_bonus = (
            TICKET_COMPLETION_REWARD
            + UNPLANNED_URGENCY_TIEBREAKER[ticket.urgency]
            + defer_penalty
        )
        score_delta = (
            extra_travel * travel_penalty
            + ticket.service_minutes
            + extra_route_work_overflow_penalty
            - priority_bonus
        )
        return Insertion(
            technician_id=route.technician.id,
            position=position,
            extra_travel_minutes=extra_travel,
            extra_distance_km=extra_distance,
            score_delta=score_delta,
        )

    def _route_travel_and_distance(self, route: MechanicRoute) -> tuple[int, float]:
        timeline = self._route_timeline(route, include_return_home=True)
        if timeline is None:
            return 24 * 60, 0.0
        return (
            sum(item.travel_minutes for item in timeline if isinstance(item, PlannedTravel)),
            sum(item.distance_km for item in timeline if isinstance(item, PlannedTravel)),
        )

    def _is_solution_hard_feasible(self, solution: PlanningSolution) -> bool:
        for route in solution.routes.values():
            timeline = self._route_timeline(route, include_return_home=True)
            if timeline is None:
                return False

            non_urgent_minutes = 0
            route_work_minutes = self._route_work_minutes(timeline)
            ticket_items = [item for item in timeline if isinstance(item, PlannedStop)]
            for item in ticket_items:
                if item.ticket.urgency != TicketUrgency.URGENT:
                    non_urgent_minutes += item.ticket.service_minutes

            if timeline:
                route_end = timeline[-1].planned_end_at
            else:
                route_end = planning_day_start(self.config, route.technician)
            if route_end > planning_day_end(self.config, route.technician):
                return False
            if non_urgent_minutes > self.config.initial_non_urgent_minutes_per_technician:
                return False
            if self._starts_ticket_after_latest_route_work_start(timeline):
                return False
        return True

    def _score(self, solution: PlanningSolution) -> None:
        total_travel = 0
        total_distance = 0.0
        completed = 0
        sla_misses = 0
        overtime = 0
        route_work_overflow_penalty = 0
        planned_ticket_ids: set[int] = set()

        for route in solution.routes.values():
            timeline = self._route_timeline(route, include_return_home=True)
            if timeline is None:
                # This should normally be filtered by hard feasibility, but keep
                # scoring robust for intermediate candidates.
                overtime += 24 * 60
                continue

            for item in timeline:
                if isinstance(item, PlannedTravel):
                    total_travel += item.travel_minutes
                    total_distance += item.distance_km
                elif isinstance(item, PlannedStop):
                    if item.planned_start_at > item.ticket.deadline_at:
                        sla_misses += 1
                    completed += 1
                    planned_ticket_ids.add(item.ticket.id)

            route_work_overflow_penalty += self._route_work_overflow_penalty_points(
                self._route_work_minutes(timeline)
            )

            if timeline:
                route_end = timeline[-1].planned_end_at
            else:
                route_end = planning_day_start(self.config, route.technician)
            day_end = planning_day_end(self.config, route.technician)
            if route_end > day_end:
                overtime += int((route_end - day_end).total_seconds() // 60)

        solution.unplanned_ticket_ids = {ticket.id for ticket in self.tickets if ticket.id not in planned_ticket_ids}
        defer_penalty = (
            max(0, self.config.defer_unplanned_penalty_minutes)
            * max(0, self.config.travel_penalty_per_minute)
        )
        unplanned_penalty = sum(
            UNPLANNED_TICKET_PENALTY
            + UNPLANNED_URGENCY_TIEBREAKER[self.ticket_by_id[ticket_id].urgency]
            + defer_penalty
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
            + route_work_overflow_penalty
            + total_travel * max(0, self.config.travel_penalty_per_minute)
        )

    def _route_timeline(
        self,
        route: MechanicRoute,
        *,
        include_return_home: bool,
    ) -> list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak] | None:
        current_time = planning_day_start(self.config, route.technician)
        previous_location_id = route.technician.start_location_id
        previous_ticket_id: int | None = None
        break_taken = False
        pickup_done = False
        route_supply_requirement_codes = frozenset(
            code
            for route_ticket_id in route.ticket_ids
            for code in self.ticket_by_id[route_ticket_id].supply_requirement_codes
        )
        timeline: list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak] = []

        for ticket_id in route.ticket_ids:
            ticket = self.ticket_by_id[ticket_id]
            accumulated_travel_minutes_before_ticket = 0
            accumulated_distance_km_before_ticket = 0.0

            requires_hq_pickup = False
            hq_location_id: int | None = None
            travel_minutes_to_hq = 0
            distance_km_to_hq = 0.0

            if ticket.supply_requirement_codes and not pickup_done:
                requires_hq_pickup = True
                hq_location_id = route.technician.office_location_id
                travel_minutes = self.matrix.duration(previous_location_id, hq_location_id)
                distance_km = self.matrix.distance(previous_location_id, hq_location_id)
                travel_minutes_to_hq = travel_minutes
                distance_km_to_hq = distance_km
                travel_start = current_time
                travel_end = travel_start + timedelta(minutes=travel_minutes)
                if travel_minutes > 0:
                    timeline.append(
                        PlannedTravel(
                            from_location_id=previous_location_id,
                            to_location_id=hq_location_id,
                            travel_minutes=travel_minutes,
                            distance_km=distance_km,
                            planned_start_at=travel_start,
                            planned_end_at=travel_end,
                            before_ticket_id=ticket.id,
                            after_ticket_id=previous_ticket_id,
                        )
                    )
                accumulated_travel_minutes_before_ticket += travel_minutes
                accumulated_distance_km_before_ticket += distance_km
                pickup_start = travel_end
                pickup_end = pickup_start + timedelta(minutes=self.config.requirement_pickup_duration_minutes)
                timeline.append(
                    PlannedRequirementPickup(
                        location_id=hq_location_id,
                        requirement_codes=route_supply_requirement_codes,
                        planned_start_at=pickup_start,
                        planned_end_at=pickup_end,
                        duration_minutes=self.config.requirement_pickup_duration_minutes,
                    )
                )
                current_time = pickup_end
                previous_location_id = hq_location_id
                pickup_done = True
            if not break_taken and self._must_break_before_next_job(
                current_time=current_time,
                previous_location_id=previous_location_id,
                ticket=ticket,
            ):
                break_item = self._planned_break_from(current_time)
                if break_item is None:
                    return None
                timeline.append(break_item)
                current_time = break_item.planned_end_at
                break_taken = True

            travel_minutes = self.matrix.duration(previous_location_id, ticket.location_id)
            distance_km = self.matrix.distance(previous_location_id, ticket.location_id)
            travel_minutes_hq_to_ticket = travel_minutes if requires_hq_pickup else 0
            distance_km_hq_to_ticket = distance_km if requires_hq_pickup else 0.0
            accumulated_travel_minutes_before_ticket += travel_minutes
            accumulated_distance_km_before_ticket += distance_km
            travel_start = current_time
            travel_end = travel_start + timedelta(minutes=travel_minutes)
            if travel_minutes > 0:
                timeline.append(
                    PlannedTravel(
                        from_location_id=previous_location_id,
                        to_location_id=ticket.location_id,
                        travel_minutes=travel_minutes,
                        distance_km=distance_km,
                        planned_start_at=travel_start,
                        planned_end_at=travel_end,
                        before_ticket_id=ticket.id,
                        after_ticket_id=previous_ticket_id,
                    )
                )

            start_at = travel_end
            end_at = start_at + timedelta(minutes=ticket.service_minutes)
            timeline.append(
                PlannedStop(
                    ticket=ticket,
                    travel_minutes_before=accumulated_travel_minutes_before_ticket,
                    distance_km_before=accumulated_distance_km_before_ticket,
                    planned_start_at=start_at,
                    planned_end_at=end_at,
                    requires_hq_pickup=requires_hq_pickup,
                    hq_location_id=hq_location_id,
                    travel_minutes_to_hq=travel_minutes_to_hq,
                    distance_km_to_hq=distance_km_to_hq,
                    travel_minutes_hq_to_ticket=travel_minutes_hq_to_ticket,
                    distance_km_hq_to_ticket=distance_km_hq_to_ticket,
                )
            )
            current_time = end_at
            previous_location_id = ticket.location_id
            previous_ticket_id = ticket.id

        if not break_taken:
            break_item = self._planned_break_from(current_time)
            if break_item is None:
                return None
            timeline.append(break_item)
            current_time = break_item.planned_end_at
            break_taken = True

        if include_return_home:
            travel_minutes = self.matrix.duration(previous_location_id, route.technician.end_location_id)
            distance_km = self.matrix.distance(previous_location_id, route.technician.end_location_id)
            travel_start = current_time
            travel_end = travel_start + timedelta(minutes=travel_minutes)
            if travel_minutes > 0:
                timeline.append(
                    PlannedTravel(
                        from_location_id=previous_location_id,
                        to_location_id=route.technician.end_location_id,
                        travel_minutes=travel_minutes,
                        distance_km=distance_km,
                        planned_start_at=travel_start,
                        planned_end_at=travel_end,
                        before_ticket_id=None,
                        after_ticket_id=previous_ticket_id,
                    )
                )
        return timeline


    def _starts_ticket_after_latest_route_work_start(
        self,
        timeline: list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak],
    ) -> bool:
        """Return True when a route begins a ticket after the protected buffer starts.

        The 6h route-work target is intentionally soft: a ticket that started
        before the protected buffer may finish slightly over target. What we do
        not want is starting another ticket once the mechanic is already in the
        reserved part of the day. Breaks do not count as route work.
        """
        latest_start = max(0, self.config.latest_ticket_start_route_work_minutes)
        worked_minutes = 0
        for item in timeline:
            if isinstance(item, PlannedStop):
                if worked_minutes >= latest_start:
                    return True
                worked_minutes += item.ticket.service_minutes
            elif isinstance(item, PlannedTravel):
                worked_minutes += item.travel_minutes
            elif isinstance(item, PlannedRequirementPickup):
                worked_minutes += item.duration_minutes
        return False

    def _route_work_overflow_penalty_points(self, route_work_minutes: int) -> int:
        """Soft, increasing penalty for ending above the 6h route-work target."""
        target = max(0, self.config.initial_route_work_minutes_per_technician)
        overflow = max(0, route_work_minutes - target)
        if overflow == 0:
            return 0

        travel_penalty = max(1, self.config.travel_penalty_per_minute)
        # First few overflow minutes are allowed but not free. The quadratic
        # term makes every additional minute more expensive than the previous
        # one, so 6h05 can win while 6h45 usually will not.
        return int(overflow * travel_penalty + (overflow * overflow * travel_penalty) / 10)

    def _route_work_minutes(
        self,
        timeline: list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak],
    ) -> int:
        """Minutes counted against the initial 5-6h route workload target.

        Lunch is excluded, but service, all driving legs, HQ pickup duration, and
        return-home travel are included. This keeps average urgent-ticket room
        available without letting long travel routes look artificially light.
        """
        return sum(
            item.travel_minutes
            if isinstance(item, PlannedTravel)
            else item.ticket.service_minutes
            if isinstance(item, PlannedStop)
            else item.duration_minutes
            if isinstance(item, PlannedRequirementPickup)
            else 0
            for item in timeline
        )

    def _must_break_before_next_job(
        self,
        *,
        current_time: datetime,
        previous_location_id: int,
        ticket: TicketInput,
    ) -> bool:
        break_start, break_end = self._break_window()
        latest_break_start = break_end - timedelta(minutes=self.config.break_duration_minutes)
        travel_minutes = self.matrix.duration(previous_location_id, ticket.location_id)
        ticket_end = current_time + timedelta(minutes=travel_minutes + ticket.service_minutes)

        earliest_after_ticket = max(ticket_end, break_start)
        can_break_after_ticket = earliest_after_ticket <= latest_break_start
        if can_break_after_ticket:
            return False

        earliest_before_ticket = max(current_time, break_start)
        return earliest_before_ticket <= latest_break_start

    def _planned_break_from(self, current_time: datetime) -> PlannedBreak | None:
        break_start, break_end = self._break_window()
        latest_break_start = break_end - timedelta(minutes=self.config.break_duration_minutes)
        start_at = max(current_time, break_start)
        if start_at > latest_break_start:
            return None
        return PlannedBreak(
            planned_start_at=start_at,
            planned_end_at=start_at + timedelta(minutes=self.config.break_duration_minutes),
            duration_minutes=self.config.break_duration_minutes,
        )

    def _break_window(self) -> tuple[datetime, datetime]:
        return (
            self._datetime_at_minutes(self.config.break_window_start_minutes),
            self._datetime_at_minutes(self.config.break_window_end_minutes),
        )

    def _datetime_at_minutes(self, minutes_after_midnight: int) -> datetime:
        return datetime.combine(
            self.config.planned_date.date(),
            time.min,
            tzinfo=self.config.planned_date.tzinfo,
        ).replace(
            hour=minutes_after_midnight // 60,
            minute=minutes_after_midnight % 60,
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

    def _planning_class_rank(self, ticket: TicketInput) -> int:
        return 0 if ticket.urgency == TicketUrgency.URGENT else 1

    def _strict_priority_key(self, ticket: TicketInput) -> tuple[int, int, datetime, int]:
        return (
            self._planning_class_rank(ticket),
            -(len(ticket.requirement_codes) + len(ticket.supply_requirement_codes)),
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
