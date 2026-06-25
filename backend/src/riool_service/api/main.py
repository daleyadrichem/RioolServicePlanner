from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from riool_service.database.db_utils import get_session
from riool_service.services.simulator_service import service as simulator_service

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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/simulator/scenarios")
def list_scenarios() -> list[dict]:
    return simulator_service.list_scenarios()


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
