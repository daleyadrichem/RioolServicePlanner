from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import combinations
import json
from pathlib import Path
from typing import Any

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
        debug_log_path: str | Path | None = None,
        debug_label: str | None = None,
    ) -> None:
        self.config = config
        self.technicians = technicians
        self.tickets = tickets
        self.ticket_by_id = {ticket.id: ticket for ticket in tickets}
        self.matrix = matrix
        self.random = random.Random(config.random_seed)
        self.debug_log_path = Path(debug_log_path) if debug_log_path is not None else None
        self.debug_label = debug_label or f"planning_date={config.planned_date.date().isoformat()}"

    def _debug(self, message: str, **fields: Any) -> None:
        """Append high-volume optimizer diagnostics to the per-planning-run log file.

        This intentionally bypasses the normal Python logger: these messages are
        verbose enough to be useful for explaining optimizer choices, but too
        noisy for the application log stream.
        """
        if self.debug_log_path is None:
            return
        self.debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "label": self.debug_label,
            "message": message,
            **fields,
        }
        line = json.dumps(payload, default=str, sort_keys=True)
        print(f"[planning-ai-debug] {line}", flush=True)
        with self.debug_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def optimize(self) -> PlanningSolution:
        self._debug(
            "optimizer_started",
            config=self._debug_config_dict(),
            technician_count=len(self.technicians),
            ticket_count=len(self.tickets),
            technicians=[self._debug_technician_dict(technician) for technician in self.technicians],
            tickets=[self._debug_ticket_dict(ticket) for ticket in self.tickets],
        )
        if not self.technicians:
            raise ValueError("At least one technician is required")
        if not self.tickets:
            solution = self._empty_solution()
            self._score(solution)
            self._debug("optimizer_finished_empty", score_breakdown=self._score_breakdown(solution))
            return solution

        best: PlanningSolution | None = None
        iterations = max(1, self.config.multi_start_iterations)
        for iteration in range(iterations):
            self._debug("planning_candidate_started", candidate_number=iteration + 1, total_candidates=iterations)
            solution = self._build_initial_solution(iteration=iteration)
            solution = self._improve(solution)
            self._score(solution)
            score_breakdown = self._score_breakdown(solution)
            self._debug(
                "planning_candidate_finished",
                candidate_number=iteration + 1,
                total_candidates=iterations,
                total_cost_for_found_planning=solution.score,
                score_breakdown=score_breakdown,
                route_summary=self._debug_solution_routes(solution),
            )
            if best is None or solution.score < best.score:
                previous_best = None if best is None else best.score
                best = solution
                self._debug(
                    "planning_candidate_became_best",
                    candidate_number=iteration + 1,
                    previous_best_cost=previous_best,
                    new_best_cost=solution.score,
                )

        assert best is not None
        best.algorithm_notes = [
            "Multi-start randomized cheapest insertion",
            "Local search with move, swap and 2-opt reorder operators",
            "Medium and low tickets are treated as the same planning class; medium only receives a tiny score tie-breaker.",
            "A 45 minute break is planned for every mechanic inside the 11:00-13:00 window",
            "If a route contains tickets with requirements, a single HQ pickup is inserted before the first required ticket.",
            "Initial route workload is capped using service + travel + HQ pickup time, so urgent-ticket capacity remains free.",
            "Deadline misses are scored softly instead of blocking otherwise efficient medium/low plans.",
            "Today's travel minutes are weighted more heavily than future-day travel minutes in multi-day planning.",
            "In multi-day planning, non-final days treat leftover tickets as deferred rather than truly unplanned.",
            "Travel time can outweigh the small medium-over-low tie-breaker.",
        ]
        self._debug(
            "optimizer_finished",
            selected_total_cost=best.score,
            selected_score_breakdown=self._score_breakdown(best),
            selected_routes=self._debug_solution_routes(best),
        )
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
        self._debug(
            "initial_solution_build_started",
            iteration=iteration,
            ordered_ticket_ids=[ticket.id for ticket in remaining],
        )

        self._seed_routes(solution, remaining, iteration=iteration)

        while remaining:
            ticket = remaining.pop(0)
            insertion = self._best_insertion(solution, ticket, allow_low_priority=True)
            if insertion is None:
                solution.unplanned_ticket_ids.add(ticket.id)
                self._debug(
                    "ticket_left_unplanned_during_initial_build",
                    iteration=iteration,
                    ticket=self._debug_ticket_dict(ticket),
                    reason="No feasible or cost-improving insertion was available for this planning day.",
                )
                continue
            solution.routes[insertion.technician_id].ticket_ids.insert(insertion.position, ticket.id)
            self._debug(
                "ticket_inserted_during_initial_build",
                iteration=iteration,
                ticket_id=ticket.id,
                selected_technician_id=insertion.technician_id,
                selected_position=insertion.position,
                extra_travel_minutes=insertion.extra_travel_minutes,
                extra_distance_km=insertion.extra_distance_km,
                score_delta=insertion.score_delta,
                routes=self._debug_solution_routes(solution),
            )

        self._score(solution)
        self._debug(
            "initial_solution_build_finished",
            iteration=iteration,
            score_breakdown=self._score_breakdown(solution),
            routes=self._debug_solution_routes(solution),
        )
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
        self._debug("local_search_started", initial_cost=best.score)
        for local_iteration in range(max(0, self.config.local_search_iterations)):
            changed = False
            for candidate in self._move_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    self._debug(
                        "local_search_improvement",
                        iteration=local_iteration + 1,
                        operator="move",
                        previous_cost=best.score,
                        new_cost=candidate.score,
                        score_breakdown=self._score_breakdown(candidate),
                    )
                    best = candidate
                    changed = True
                    break
            if changed:
                continue

            for candidate in self._swap_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    self._debug(
                        "local_search_improvement",
                        iteration=local_iteration + 1,
                        operator="swap",
                        previous_cost=best.score,
                        new_cost=candidate.score,
                        score_breakdown=self._score_breakdown(candidate),
                    )
                    best = candidate
                    changed = True
                    break
            if changed:
                continue

            for candidate in self._two_opt_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    self._debug(
                        "local_search_improvement",
                        iteration=local_iteration + 1,
                        operator="two_opt",
                        previous_cost=best.score,
                        new_cost=candidate.score,
                        score_breakdown=self._score_breakdown(candidate),
                    )
                    best = candidate
                    changed = True
                    break
            if changed:
                continue

            # Repair the main blind spot of cheapest insertion: a ticket that was
            # left unplanned/deferred earlier can be a better local fit than one
            # of the tickets that happened to be inserted first. This operator
            # swaps one planned ticket with one currently-unplanned ticket and
            # reinserts the incoming ticket in the same technician route.
            for candidate in self._unplanned_replacement_candidates(best):
                self._score(candidate)
                if candidate.score < best.score:
                    self._debug(
                        "local_search_improvement",
                        iteration=local_iteration + 1,
                        operator="unplanned_replacement",
                        previous_cost=best.score,
                        new_cost=candidate.score,
                        score_breakdown=self._score_breakdown(candidate),
                    )
                    best = candidate
                    changed = True
                    break
            if not changed:
                self._debug("local_search_stopped_no_improvement", iteration=local_iteration + 1, best_cost=best.score)
                break
        self._debug("local_search_finished", final_cost=best.score)
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

    def _unplanned_replacement_candidates(self, solution: PlanningSolution):
        """Swap one planned ticket with one unplanned/deferred ticket.

        The normal local-search operators only move/swap/reorder tickets that are
        already planned for this day. On non-final horizon days, an unplanned
        ticket is usually just deferred to tomorrow, so replacing a planned low
        ticket with a geographically better deferred low ticket can reduce travel
        without changing daily capacity or SLA risk.
        """
        if not solution.unplanned_ticket_ids:
            return

        unplanned_ids = list(solution.unplanned_ticket_ids)
        self.random.shuffle(unplanned_ids)

        route_ids = list(solution.routes)
        self.random.shuffle(route_ids)
        for technician_id in route_ids:
            route = solution.routes[technician_id]
            planned_positions = list(range(len(route.ticket_ids)))
            self.random.shuffle(planned_positions)
            for planned_position in planned_positions:
                outgoing_ticket = self.ticket_by_id[route.ticket_ids[planned_position]]
                for incoming_ticket_id in unplanned_ids:
                    incoming_ticket = self.ticket_by_id[incoming_ticket_id]
                    if not self._can_do(route.technician, incoming_ticket):
                        continue
                    if not self._can_replace_planned_with_unplanned(outgoing_ticket, incoming_ticket):
                        continue

                    # Remove the outgoing ticket, then try every insertion point
                    # for the incoming ticket in the same route. This allows both
                    # a direct replacement and a small within-route reorder.
                    for insert_position in range(len(route.ticket_ids)):
                        candidate = solution.copy()
                        candidate_route = candidate.routes[technician_id]
                        candidate_route.ticket_ids.pop(planned_position)
                        candidate_route.ticket_ids.insert(insert_position, incoming_ticket_id)
                        if self._is_solution_hard_feasible(candidate):
                            yield candidate

    def _can_replace_planned_with_unplanned(
        self,
        outgoing_ticket: TicketInput,
        incoming_ticket: TicketInput,
    ) -> bool:
        """Protect urgent/medium SLA work while allowing low-vs-low repair.

        A low ticket may not replace urgent/medium work. Medium/urgent replacements
        are only allowed when the incoming ticket is at least as urgent and does
        not have a later deadline. Low-vs-low is intentionally open because the
        defer cost is equal and route efficiency should decide.
        """
        if incoming_ticket.id == outgoing_ticket.id:
            return False
        if incoming_ticket.urgency_rank > outgoing_ticket.urgency_rank:
            return False
        if outgoing_ticket.urgency != TicketUrgency.LOW and incoming_ticket.deadline_at > outgoing_ticket.deadline_at:
            return False
        return True

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
        allow_non_improving: bool = False,
    ) -> Insertion | None:
        best: Insertion | None = None
        candidate_technician_ids = technician_ids or list(solution.routes)
        self._debug(
            "best_insertion_started",
            ticket=self._debug_ticket_dict(ticket),
            candidate_technician_ids=candidate_technician_ids,
            allow_low_priority=allow_low_priority,
            allow_non_improving=allow_non_improving,
            current_routes=self._debug_solution_routes(solution),
        )
        for technician_id in candidate_technician_ids:
            route = solution.routes[technician_id]
            if not self._can_do(route.technician, ticket):
                self._debug(
                    "insertion_candidate_rejected",
                    ticket_id=ticket.id,
                    technician_id=technician_id,
                    reason="technician_missing_required_skills",
                    missing_requirements=sorted(ticket.requirement_codes - route.technician.requirement_codes),
                    technician_requirements=sorted(route.technician.requirement_codes),
                )
                continue
            for position in range(len(route.ticket_ids) + 1):
                insertion = self._evaluate_insertion(solution, route, ticket, position)
                if insertion is None:
                    continue
                if ticket.is_low_priority and not allow_low_priority:
                    self._debug(
                        "insertion_candidate_rejected",
                        ticket_id=ticket.id,
                        technician_id=technician_id,
                        position=position,
                        reason="low_priority_not_allowed_in_this_search",
                    )
                    continue
                # Hard feasibility protects workday, skill, lunch-break and
                # daily non-urgent capacity rules. The score-delta check below
                # decides whether the best feasible insertion is worth doing on
                # this day or should be deferred to a later horizon day.
                if best is None or insertion.score_delta < best.score_delta:
                    best = insertion

        # For non-final horizon days, leaving a ticket off the active day means
        # deferring it, not losing it. Only insert work when it improves the
        # active day's own objective: heavy same-day travel cost versus the
        # configured defer penalty. The final horizon day still has the large
        # true-unplanned base penalty, so any feasible ticket remains strongly
        # preferred there.
        if (
            best is not None
            and not allow_non_improving
            and not self.config.apply_unplanned_base_penalty
            and best.score_delta >= 0
        ):
            self._debug(
                "best_insertion_rejected_by_defer_policy",
                ticket_id=ticket.id,
                best_candidate=self._debug_insertion_dict(best),
                reason=(
                    "Best insertion did not beat the active-day defer penalty; "
                    "ticket is deferred to a later horizon day instead."
                ),
            )
            return None
        self._debug(
            "best_insertion_finished",
            ticket_id=ticket.id,
            selected_insertion=self._debug_insertion_dict(best) if best is not None else None,
            reason="selected_lowest_score_delta_feasible_position" if best is not None else "no_feasible_position_found",
        )
        return best

    def _effective_travel_penalty_per_minute(self) -> float:
        """Travel penalty after applying the active-day multiplier.

        Multi-day planning uses this to make today route-efficient without
        over-optimizing tomorrow/the day after, since future tickets may still
        change as new work comes in during the day.
        """
        return max(0, self.config.travel_penalty_per_minute) * max(0.0, self.config.active_day_travel_penalty_multiplier)

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
        feasibility_reasons = self._hard_feasibility_reasons(candidate)
        if feasibility_reasons:
            self._debug(
                "insertion_candidate_rejected",
                ticket_id=ticket.id,
                technician_id=route.technician.id,
                position=position,
                reason="hard_feasibility_failed",
                feasibility_reasons=feasibility_reasons,
                route_ticket_ids=candidate_route.ticket_ids,
            )
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
        travel_penalty = self._effective_travel_penalty_per_minute()
        defer_penalty = self._defer_unplanned_penalty_points()
        unplanned_base_penalty = TICKET_COMPLETION_REWARD if self.config.apply_unplanned_base_penalty else 0
        priority_bonus = (
            unplanned_base_penalty
            + UNPLANNED_URGENCY_TIEBREAKER[ticket.urgency]
            + defer_penalty
        )
        score_delta = (
            extra_travel * travel_penalty
            + ticket.service_minutes
            + extra_route_work_overflow_penalty
            - priority_bonus
        )
        self._debug(
            "insertion_candidate_evaluated",
            ticket_id=ticket.id,
            technician_id=route.technician.id,
            position=position,
            old_travel_minutes=old_travel,
            new_travel_minutes=new_travel,
            extra_travel_minutes=extra_travel,
            old_distance_km=old_distance,
            new_distance_km=new_distance,
            extra_distance_km=extra_distance,
            old_route_work_minutes=old_route_work,
            new_route_work_minutes=new_route_work,
            extra_route_work_overflow_penalty=extra_route_work_overflow_penalty,
            travel_penalty_per_minute=self.config.travel_penalty_per_minute,
            active_day_travel_multiplier=self.config.active_day_travel_penalty_multiplier,
            effective_travel_penalty_per_minute=travel_penalty,
            defer_penalty_points=defer_penalty,
            unplanned_base_penalty_points=unplanned_base_penalty,
            urgency_tiebreaker_points=UNPLANNED_URGENCY_TIEBREAKER[ticket.urgency],
            priority_bonus_points=priority_bonus,
            service_minutes_cost=ticket.service_minutes,
            score_delta_formula=(
                "extra_travel * effective_travel_penalty_per_minute "
                "+ service_minutes + extra_route_work_overflow_penalty - priority_bonus"
            ),
            score_delta=score_delta,
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
        return not self._hard_feasibility_reasons(solution)

    def _hard_feasibility_reasons(self, solution: PlanningSolution) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        for route in solution.routes.values():
            timeline = self._route_timeline(route, include_return_home=True)
            if timeline is None:
                reasons.append(
                    {
                        "technician_id": route.technician.id,
                        "technician_name": route.technician.name,
                        "route_ticket_ids": route.ticket_ids,
                        "reason": "timeline_could_not_be_built",
                        "detail": "Usually caused by inability to place the mandatory lunch break inside the configured break window.",
                    }
                )
                continue

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
                reasons.append(
                    {
                        "technician_id": route.technician.id,
                        "technician_name": route.technician.name,
                        "route_ticket_ids": route.ticket_ids,
                        "reason": "route_ends_after_workday",
                        "route_end": route_end.isoformat(),
                        "workday_end": planning_day_end(self.config, route.technician).isoformat(),
                        "overtime_minutes": int((route_end - planning_day_end(self.config, route.technician)).total_seconds() // 60),
                    }
                )
            if non_urgent_minutes > self.config.initial_non_urgent_minutes_per_technician:
                reasons.append(
                    {
                        "technician_id": route.technician.id,
                        "technician_name": route.technician.name,
                        "route_ticket_ids": route.ticket_ids,
                        "reason": "non_urgent_minutes_cap_exceeded",
                        "non_urgent_minutes": non_urgent_minutes,
                        "cap_minutes": self.config.initial_non_urgent_minutes_per_technician,
                    }
                )
            if self._starts_ticket_after_latest_route_work_start(timeline):
                reasons.append(
                    {
                        "technician_id": route.technician.id,
                        "technician_name": route.technician.name,
                        "route_ticket_ids": route.ticket_ids,
                        "reason": "ticket_starts_after_latest_route_work_start",
                        "route_work_minutes": route_work_minutes,
                        "latest_ticket_start_route_work_minutes": self.config.latest_ticket_start_route_work_minutes,
                    }
                )
        return reasons

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
        defer_penalty = self._defer_unplanned_penalty_points()
        unplanned_base_penalty = UNPLANNED_TICKET_PENALTY if self.config.apply_unplanned_base_penalty else 0
        unplanned_penalty = sum(
            unplanned_base_penalty
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
            + total_travel * self._effective_travel_penalty_per_minute()
        )

    def _score_breakdown(self, solution: PlanningSolution) -> dict[str, Any]:
        travel_penalty = self._effective_travel_penalty_per_minute()
        defer_penalty = self._defer_unplanned_penalty_points()
        unplanned_base_penalty = UNPLANNED_TICKET_PENALTY if self.config.apply_unplanned_base_penalty else 0
        route_work_overflow_penalty = 0
        route_breakdowns: list[dict[str, Any]] = []
        for route in solution.routes.values():
            timeline = self._route_timeline(route, include_return_home=True)
            if timeline is None:
                route_breakdowns.append(
                    {
                        "technician_id": route.technician.id,
                        "technician_name": route.technician.name,
                        "route_ticket_ids": route.ticket_ids,
                        "timeline_feasible": False,
                        "fallback_overtime_minutes": 24 * 60,
                    }
                )
                continue
            route_work = self._route_work_minutes(timeline)
            route_overflow_penalty = self._route_work_overflow_penalty_points(route_work)
            route_work_overflow_penalty += route_overflow_penalty
            travel_items = [item for item in timeline if isinstance(item, PlannedTravel)]
            stop_items = [item for item in timeline if isinstance(item, PlannedStop)]
            route_breakdowns.append(
                {
                    "technician_id": route.technician.id,
                    "technician_name": route.technician.name,
                    "route_ticket_ids": route.ticket_ids,
                    "timeline_feasible": True,
                    "travel_minutes": sum(item.travel_minutes for item in travel_items),
                    "travel_cost": sum(item.travel_minutes for item in travel_items) * travel_penalty,
                    "distance_km": round(sum(item.distance_km for item in travel_items), 3),
                    "service_minutes": sum(item.ticket.service_minutes for item in stop_items),
                    "route_work_minutes": route_work,
                    "route_work_overflow_penalty": route_overflow_penalty,
                    "sla_miss_ticket_ids": [item.ticket.id for item in stop_items if item.planned_start_at > item.ticket.deadline_at],
                    "timeline": self._debug_timeline(timeline),
                }
            )
        unplanned_details = []
        for ticket_id in sorted(solution.unplanned_ticket_ids):
            ticket = self.ticket_by_id[ticket_id]
            urgency_tiebreaker = UNPLANNED_URGENCY_TIEBREAKER[ticket.urgency]
            unplanned_details.append(
                {
                    "ticket_id": ticket_id,
                    "urgency": ticket.urgency.value,
                    "unplanned_base_penalty": unplanned_base_penalty,
                    "urgency_tiebreaker": urgency_tiebreaker,
                    "defer_penalty": defer_penalty,
                    "defer_penalty_formula": "defer_unplanned_penalty_minutes * effective_travel_penalty_per_minute",
                    "ticket_unplanned_cost": unplanned_base_penalty + urgency_tiebreaker + defer_penalty,
                }
            )
        travel_cost = solution.total_travel_minutes * travel_penalty
        sla_cost = solution.sla_misses * SLA_MISS_PENALTY
        overtime_cost = solution.overtime_minutes * OVERTIME_PENALTY_PER_MINUTE
        unplanned_cost = sum(item["ticket_unplanned_cost"] for item in unplanned_details)
        return {
            "total_cost": solution.score,
            "formula": "sla_cost + unplanned_cost + overtime_cost + route_work_overflow_penalty + travel_cost",
            "travel_minutes": solution.total_travel_minutes,
            "travel_penalty_per_minute": self.config.travel_penalty_per_minute,
            "active_day_travel_multiplier": self.config.active_day_travel_penalty_multiplier,
            "effective_travel_penalty_per_minute": travel_penalty,
            "travel_cost": travel_cost,
            "completed_tickets": solution.completed_tickets,
            "unplanned_ticket_count": len(solution.unplanned_ticket_ids),
            "unplanned_cost": unplanned_cost,
            "unplanned_details": unplanned_details,
            "sla_misses": solution.sla_misses,
            "sla_miss_penalty_per_ticket": SLA_MISS_PENALTY,
            "sla_cost": sla_cost,
            "overtime_minutes": solution.overtime_minutes,
            "overtime_penalty_per_minute": OVERTIME_PENALTY_PER_MINUTE,
            "overtime_cost": overtime_cost,
            "route_work_overflow_penalty": route_work_overflow_penalty,
            "total_distance_km": round(solution.total_distance_km, 3),
            "routes": route_breakdowns,
        }

    def _debug_solution_routes(self, solution: PlanningSolution) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        for route in solution.routes.values():
            timeline = self._route_timeline(route, include_return_home=True)
            routes.append(
                {
                    "technician_id": route.technician.id,
                    "technician_name": route.technician.name,
                    "ticket_ids": route.ticket_ids[:],
                    "travel_minutes": (
                        sum(item.travel_minutes for item in timeline if isinstance(item, PlannedTravel))
                        if timeline is not None
                        else None
                    ),
                    "route_work_minutes": self._route_work_minutes(timeline) if timeline is not None else None,
                    "timeline_feasible": timeline is not None,
                }
            )
        return routes

    def _debug_timeline(
        self,
        timeline: list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak],
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in timeline:
            if isinstance(item, PlannedTravel):
                items.append(
                    {
                        "type": "travel",
                        "from_location_id": item.from_location_id,
                        "to_location_id": item.to_location_id,
                        "travel_minutes": item.travel_minutes,
                        "travel_cost": item.travel_minutes * self._effective_travel_penalty_per_minute(),
                        "distance_km": round(item.distance_km, 3),
                        "planned_start_at": item.planned_start_at.isoformat(),
                        "planned_end_at": item.planned_end_at.isoformat(),
                        "before_ticket_id": item.before_ticket_id,
                        "after_ticket_id": item.after_ticket_id,
                    }
                )
            elif isinstance(item, PlannedStop):
                items.append(
                    {
                        "type": "ticket",
                        "ticket_id": item.ticket.id,
                        "urgency": item.ticket.urgency.value,
                        "service_minutes": item.ticket.service_minutes,
                        "planned_start_at": item.planned_start_at.isoformat(),
                        "planned_end_at": item.planned_end_at.isoformat(),
                        "deadline_at": item.ticket.deadline_at.isoformat(),
                        "sla_miss": item.planned_start_at > item.ticket.deadline_at,
                        "travel_minutes_before": item.travel_minutes_before,
                        "travel_cost_before": item.travel_minutes_before * self._effective_travel_penalty_per_minute(),
                        "requires_hq_pickup": item.requires_hq_pickup,
                        "travel_minutes_to_hq": item.travel_minutes_to_hq,
                        "travel_minutes_hq_to_ticket": item.travel_minutes_hq_to_ticket,
                    }
                )
            elif isinstance(item, PlannedRequirementPickup):
                items.append(
                    {
                        "type": "requirement_pickup",
                        "location_id": item.location_id,
                        "requirement_codes": sorted(item.requirement_codes),
                        "duration_minutes": item.duration_minutes,
                        "planned_start_at": item.planned_start_at.isoformat(),
                        "planned_end_at": item.planned_end_at.isoformat(),
                    }
                )
            elif isinstance(item, PlannedBreak):
                items.append(
                    {
                        "type": "break",
                        "duration_minutes": item.duration_minutes,
                        "planned_start_at": item.planned_start_at.isoformat(),
                        "planned_end_at": item.planned_end_at.isoformat(),
                    }
                )
        return items

    def _debug_config_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.config.branch_id,
            "planned_date": self.config.planned_date.isoformat(),
            "initial_non_urgent_minutes_per_technician": self.config.initial_non_urgent_minutes_per_technician,
            "initial_route_work_minutes_per_technician": self.config.initial_route_work_minutes_per_technician,
            "latest_ticket_start_route_work_minutes": self.config.latest_ticket_start_route_work_minutes,
            "travel_penalty_per_minute": self.config.travel_penalty_per_minute,
            "today_travel_penalty_multiplier": self.config.today_travel_penalty_multiplier,
            "active_day_travel_penalty_multiplier": self.config.active_day_travel_penalty_multiplier,
            "effective_travel_penalty_per_minute": self._effective_travel_penalty_per_minute(),
            "planning_horizon_days": self.config.planning_horizon_days,
            "defer_unplanned_penalty_minutes": self.config.defer_unplanned_penalty_minutes,
            "defer_unplanned_penalty_uses_effective_travel_penalty": True,
            "defer_unplanned_penalty_points": self._defer_unplanned_penalty_points(),
            "apply_unplanned_base_penalty": self.config.apply_unplanned_base_penalty,
            "multi_start_iterations": self.config.multi_start_iterations,
            "local_search_iterations": self.config.local_search_iterations,
            "random_seed": self.config.random_seed,
            "break_duration_minutes": self.config.break_duration_minutes,
            "requirement_pickup_duration_minutes": self.config.requirement_pickup_duration_minutes,
        }

    def _debug_technician_dict(self, technician: TechnicianInput) -> dict[str, Any]:
        return {
            "id": technician.id,
            "name": technician.name,
            "start_location_id": technician.start_location_id,
            "end_location_id": technician.end_location_id,
            "workday_start_minutes": technician.workday_start_minutes,
            "workday_end_minutes": technician.workday_end_minutes,
            "requirement_codes": sorted(technician.requirement_codes),
            "office_location_id": technician.office_location_id,
        }

    def _debug_ticket_dict(self, ticket: TicketInput) -> dict[str, Any]:
        return {
            "id": ticket.id,
            "location_id": ticket.location_id,
            "urgency": ticket.urgency.value,
            "deadline_at": ticket.deadline_at.isoformat(),
            "created_at": ticket.created_at.isoformat(),
            "service_minutes": ticket.service_minutes,
            "requirement_codes": sorted(ticket.requirement_codes),
            "supply_requirement_codes": sorted(ticket.supply_requirement_codes),
            "subject": ticket.subject,
            "address": ticket.address,
        }

    def _debug_insertion_dict(self, insertion: Insertion | None) -> dict[str, Any] | None:
        if insertion is None:
            return None
        return {
            "technician_id": insertion.technician_id,
            "position": insertion.position,
            "extra_travel_minutes": insertion.extra_travel_minutes,
            "extra_distance_km": insertion.extra_distance_km,
            "score_delta": insertion.score_delta,
        }

    def _route_timeline(
        self,
        route: MechanicRoute,
        *,
        include_return_home: bool,
    ) -> list[PlannedStop | PlannedTravel | PlannedRequirementPickup | PlannedBreak] | None:
        current_time = planning_day_start(self.config, route.technician)
        previous_location_id = route.technician.start_location_id
        previous_ticket_id: int | None = None
        break_start, break_end = self._break_window()
        latest_break_start = break_end - timedelta(minutes=self.config.break_duration_minutes)
        # Operational replanning can start a technician mid-day, after fixed
        # completed/in-progress/driving tickets. If that start time is already
        # past the last possible lunch-break start, treat lunch as already
        # handled outside the replanned remainder of the day. Otherwise every
        # candidate solution becomes globally infeasible even when the ticket
        # being evaluated is assigned to another technician.
        break_taken = current_time > latest_break_start
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

    def _defer_unplanned_penalty_points(self) -> int:
        return int(
            max(0, self.config.defer_unplanned_penalty_minutes)
            * self._effective_travel_penalty_per_minute()
        )

    def _strict_priority_key(self, ticket: TicketInput) -> tuple[int, datetime, int]:
        return (
            self._planning_class_rank(ticket),
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
