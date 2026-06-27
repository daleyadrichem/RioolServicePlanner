from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Date, DateTime, Enum as SqlEnum, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from riool_service.database.models.base import Base


class SimulationStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class SimulationState(Base):
    """Single-row table that stores the simulator clock and controls.

    The simulator worker reads this row continuously. The FastAPI endpoints only
    update this state; they do not run the simulation clock themselves.
    """

    __tablename__ = "simulation_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[SimulationStatus] = mapped_column(
        SqlEnum(SimulationStatus, name="simulation_status"),
        default=SimulationStatus.IDLE,
        nullable=False,
        index=True,
    )

    simulation_date: Mapped[datetime] = mapped_column(Date, nullable=False)
    current_simulation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    day_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    day_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    speed_multiplier: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_tick_real_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    activity_log: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"SimulationState(id={self.id!r}, scenario_id={self.scenario_id!r}, "
            f"status={self.status!r}, current_simulation_time={self.current_simulation_time!r})"
        )
