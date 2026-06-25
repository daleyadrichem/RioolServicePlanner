from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .branch import Branch
    from .planning_assignment import PlanningAssignment


class PlanningRunTrigger(str, Enum):
    DAILY_START = "DAILY_START"
    NEW_TICKET = "NEW_TICKET"
    NEW_URGENT_TICKET = "NEW_URGENT_TICKET"
    PLANNER_INTERVENTION = "PLANNER_INTERVENTION"
    TECHNICIAN_UNAVAILABLE = "TECHNICIAN_UNAVAILABLE"
    SIMULATOR_EVENT = "SIMULATOR_EVENT"


class PlanningRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PlanningRun(Base):
    __tablename__ = "planning_runs"

    id: Mapped[int] = mapped_column(primary_key=True)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id"),
        nullable=False,
        index=True,
    )

    trigger_type: Mapped[PlanningRunTrigger] = mapped_column(
        SqlEnum(PlanningRunTrigger, name="planning_run_trigger"),
        nullable=False,
        index=True,
    )

    status: Mapped[PlanningRunStatus] = mapped_column(
        SqlEnum(PlanningRunStatus, name="planning_run_status"),
        default=PlanningRunStatus.PENDING,
        nullable=False,
        index=True,
    )

    planned_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    score_total_distance_km: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    score_total_travel_minutes: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    score_completed_tickets: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    score_unplanned_tickets: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    branch: Mapped[Branch] = relationship(
        "Branch",
        back_populates="planning_runs",
    )

    assignments: Mapped[list[PlanningAssignment]] = relationship(
        "PlanningAssignment",
        back_populates="planning_run",
    )

    def __repr__(self) -> str:
        return (
            f"PlanningRun(id={self.id!r}, "
            f"branch_id={self.branch_id!r}, "
            f"trigger_type={self.trigger_type!r}, "
            f"status={self.status!r})"
        )
