from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from riool_service.database.db_utils import get_session
from riool_service.services.simulator_service import service as simulator_service

SessionDep = Annotated[Session, Depends(get_session)]

app = FastAPI(title="Riool Service Planner API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://127.0.0.1:4173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/simulator/scenarios")
def list_scenarios() -> list[dict]:
    return simulator_service.list_scenarios()


@app.post("/simulator/generate-tickets")
def generate_tickets(
    scenario_id: str = Query("normale_dag"),
    seed: int | None = Query(default=None),
) -> dict:
    try:
        return simulator_service.generate_scenario_tickets(scenario_id=scenario_id, seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/simulator/injections")
def list_injections(session: SessionDep) -> list[dict]:
    return simulator_service.list_planned_injections(session)


@app.get("/simulator/state")
def get_state(session: SessionDep) -> dict:
    return simulator_service.get_state(session)


@app.post("/simulator/start")
def start_simulation() -> dict:
    return simulator_service.start()


@app.post("/simulator/pause")
def pause_simulation() -> dict:
    return simulator_service.pause()


@app.post("/simulator/reset")
def reset_simulation(session: SessionDep) -> dict:
    return simulator_service.reset(session)


@app.post("/simulator/step")
def step_simulation(session: SessionDep, minutes: int = Query(default=15, ge=1, le=480)) -> dict:
    result = simulator_service.step(session, minutes=minutes)
    session.commit()
    return result


@app.delete("/simulator/injections/{injection_id}")
def delete_injection(injection_id: int, session: SessionDep) -> dict:
    try:
        result = simulator_service.delete_injection(session, injection_id)
        session.commit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
