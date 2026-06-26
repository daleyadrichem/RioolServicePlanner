from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from riool_service.database.db_utils import get_session
from riool_service.services.simulator_service import service as simulator_service
from riool_service.services.ticket_service import service as ticket_service
from riool_service.services.routing import service as routing_service
from riool_service.services.planning_ai import service as planning_ai_service
from riool_service.services.map_service import get_map_overview

SessionDep = Annotated[Session, Depends(get_session)]


class SimulationTicketPayload(BaseModel):
    inject_time: str | None = None
    inject_at: str | None = None
    urgency: str = "medium"
    subject: str
    address: str
    city: str | None = "Den Bosch"
    requires_ladder: bool = False
    requires_spring: bool = False
    requirements: list[str] = []
    description: str | None = None
    location_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict()


class TicketPayload(BaseModel):
    urgency: str = "medium"
    subject: str | None = None
    address: str | None = None
    city: str | None = None
    branch_id: int | None = None
    branch_name: str | None = None
    requires_ladder: bool = False
    requires_spring: bool = False
    requirements: list[str] = []
    description: str | None = None
    location_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    status: str | None = None

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict(exclude_unset=True)



class RoutingTicketSelectionPayload(BaseModel):
    ticket_ids: list[int]
    start_ticket_id: int | None = None
    end_ticket_id: int | None = None
    refresh_cache: bool = False

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict()


class RoutingTicketMatrixBetweenPayload(BaseModel):
    source_ticket_ids: list[int]
    destination_ticket_ids: list[int]
    refresh_cache: bool = False

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict()



class InitialPlanningPayload(BaseModel):
    branch_id: int = 1
    planned_date: str | None = None
    refresh_route_cache: bool = False
    max_candidates_per_technician: int = 0
    requirement_pickup_duration_minutes: int = 5
    initial_non_urgent_minutes_per_technician: int = 360
    planning_horizon_days: int = 3
    default_service_minutes: int = 60
    multi_start_iterations: int = 40
    local_search_iterations: int = 250
    random_seed: int | None = 42
    low_priority_max_extra_travel_minutes: int = 35

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict(exclude_unset=True)

class AddressValidationPayload(BaseModel):
    address: str
    latitude: float | None = None
    longitude: float | None = None

    def as_service_payload(self) -> dict[str, Any]:
        return self.dict()

app = FastAPI(
    title="Riool Service Planner API",
    description=(
        "API for the Riool Service Planner backend. "
        "Use the simulator endpoints to list scenarios, generate tickets, "
        "inspect planned ticket injections, and control simulation time."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    simulator_service.ensure_simulator_tables()
    ticket_service.ensure_ticket_tables()
    routing_service.ensure_routing_tables()
    planning_ai_service.ensure_planning_ai_tables()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/simulator/scenarios")
def list_scenarios() -> list[dict]:
    return simulator_service.list_scenarios()


@app.get("/branches")
def list_branches(session: SessionDep) -> list[dict]:
    return ticket_service.list_branches(session)


@app.get("/technicians")
def list_technicians(session: SessionDep) -> list[dict]:
    return ticket_service.list_technicians(session)


@app.get("/tickets")
def list_tickets(
    session: SessionDep,
    urgency: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[dict]:
    try:
        return ticket_service.list_tickets(session, urgency=urgency, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/tickets/statistics")
def get_ticket_statistics(session: SessionDep) -> dict:
    return ticket_service.get_statistics(session)


@app.post("/tickets/validate-address")
def validate_ticket_address(payload: AddressValidationPayload) -> dict:
    try:
        return simulator_service.validate_manual_address(payload.as_service_payload())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/tickets")
def create_ticket(payload: TicketPayload, session: SessionDep) -> dict:
    try:
        result = ticket_service.create_ticket(session, payload.as_service_payload())
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, payload: TicketPayload, session: SessionDep) -> dict:
    try:
        result = ticket_service.update_ticket(session, ticket_id, payload.as_service_payload())
        session.commit()
        return result
    except ticket_service.TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, session: SessionDep) -> dict:
    try:
        result = ticket_service.delete_ticket(session, ticket_id)
        session.commit()
        return result
    except ticket_service.TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/map/overview")
def map_overview(session: SessionDep, branch_id: int | None = Query(default=None)) -> dict:
    return get_map_overview(session, branch_id=branch_id)


@app.get("/planning")
def get_planning(
    session: SessionDep,
    branch_id: int | None = Query(default=None),
    planned_date: str | None = Query(default=None),
) -> dict:
    try:
        return planning_ai_service.get_planning_overview(session, branch_id=branch_id, planned_date=planned_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/planning/auto-plan")
def auto_plan(session: SessionDep, payload: InitialPlanningPayload | None = Body(default=None)) -> dict:
    try:
        data = payload.as_service_payload() if payload else InitialPlanningPayload().as_service_payload()
        result = planning_ai_service.run_initial_planning(session, data)
        session.commit()
        result["overview"] = planning_ai_service.get_planning_overview(session, branch_id=data.get("branch_id"))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/planning/replan")
def replan(session: SessionDep, payload: InitialPlanningPayload | None = Body(default=None)) -> dict:
    try:
        data = payload.as_service_payload() if payload else InitialPlanningPayload().as_service_payload()
        result = planning_ai_service.run_replanning(session, data)
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/planning/initial/proposal")
def create_initial_planning_proposal(payload: InitialPlanningPayload, session: SessionDep) -> dict:
    try:
        result = planning_ai_service.create_initial_planning_proposal(session, payload.as_service_payload())
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/planning/initial/run")
def run_initial_planning(payload: InitialPlanningPayload, session: SessionDep) -> dict:
    try:
        result = planning_ai_service.run_initial_planning(session, payload.as_service_payload())
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/routing/tickets/matrix")
def get_routing_matrix(payload: RoutingTicketSelectionPayload, session: SessionDep) -> dict:
    try:
        data = payload.as_service_payload()
        result = routing_service.get_ticket_route_matrix(
            session,
            data["ticket_ids"],
            refresh_cache=data["refresh_cache"],
        )
        session.commit()
        return result
    except routing_service.TicketRoutingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except routing_service.RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except routing_service.OsrmProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/routing/tickets/matrix-between")
def get_routing_matrix_between(payload: RoutingTicketMatrixBetweenPayload, session: SessionDep) -> dict:
    try:
        data = payload.as_service_payload()
        result = routing_service.get_ticket_route_matrix_between(
            session,
            data["source_ticket_ids"],
            data["destination_ticket_ids"],
            refresh_cache=data["refresh_cache"],
        )
        session.commit()
        return result
    except routing_service.TicketRoutingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except routing_service.RoutingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except routing_service.OsrmProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/simulator/generate-tickets")
def generate_tickets(
    session: SessionDep,
    scenario_id: str = Query("normale_dag"),
    seed: int | None = Query(default=None),
) -> dict:
    try:
        result = simulator_service.generate_scenario_tickets(session=session, scenario_id=scenario_id, seed=seed)
        session.commit()
        return result
    except simulator_service.SimulationIsRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/simulator/injections")
def list_injections(session: SessionDep) -> list[dict]:
    return simulator_service.list_planned_injections(session)


@app.post("/simulator/validate-address")
def validate_simulator_address(payload: AddressValidationPayload) -> dict:
    try:
        return simulator_service.validate_manual_address(payload.as_service_payload())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/simulator/state")
def get_state(session: SessionDep) -> dict:
    return simulator_service.get_state(session)


@app.get("/simulator/statistics")
def get_statistics(session: SessionDep) -> dict:
    return simulator_service.get_statistics(session)


@app.post("/simulator/start")
def start_simulation(session: SessionDep) -> dict:
    result = simulator_service.start(session)
    session.commit()
    return result


@app.post("/simulator/pause")
def pause_simulation(session: SessionDep) -> dict:
    result = simulator_service.pause(session)
    session.commit()
    return result


@app.post("/simulator/stop")
def stop_simulation(session: SessionDep) -> dict:
    result = simulator_service.stop(session)
    session.commit()
    return result


@app.patch("/simulator/speed")
def set_simulation_speed(session: SessionDep, speed_multiplier: int = Query(default=5, ge=1, le=150)) -> dict:
    try:
        result = simulator_service.set_speed(session, speed_multiplier=speed_multiplier)
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/simulator/injections")
def create_injection(payload: SimulationTicketPayload, session: SessionDep) -> dict:
    try:
        result = simulator_service.create_simulation_ticket(session, payload.as_service_payload())
        session.commit()
        return result
    except simulator_service.SimulationIsRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/simulator/injections/{injection_id}")
def update_injection(injection_id: int, payload: SimulationTicketPayload, session: SessionDep) -> dict:
    try:
        result = simulator_service.update_simulation_ticket(session, injection_id, payload.as_service_payload())
        session.commit()
        return result
    except simulator_service.SimulationIsRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except simulator_service.SimulationTicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/simulator/injections/{injection_id}")
def delete_injection(injection_id: int, session: SessionDep) -> dict:
    try:
        result = simulator_service.delete_injection(session, injection_id)
        session.commit()
        return result
    except simulator_service.SimulationIsRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except simulator_service.SimulationTicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
