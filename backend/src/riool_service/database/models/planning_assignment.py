from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.tickets import Base

if TYPE_CHECKING:
    from .branch import Branch
    from .planning_run import PlanningRun
    from .technician import Technician
    from .tickets import Ticket


class PlanningAssignmentStatus(str, Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    MOVED = "MOVED"


class PlanningAssignmentSource(str, Enum):
    AI = "AI"
    PLANNER = "PLANNER"
    SIMULATOR = "SIMULATOR"


class PlanningAssignment(Base):
    __tablename__ = "planning_assignments"

    __table_args__ = (
        UniqueConstraint(
            "ticket_id",
            "planning_run_id",
            name="uq_ticket_planning_run_assignment",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    planning_run_id: Mapped[int] = mapped_column(
        ForeignKey("planning_runs.id"),
        nullable=False,
        index=True,
    )

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id"),
        nullable=False,
        index=True,
    )

    technician_id: Mapped[int] = mapped_column(
        ForeignKey("technicians.id"),
        nullable=False,
        index=True,
    )

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id"),
        nullable=False,
        index=True,
    )

    sequence_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    planned_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    planned_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    estimated_duration_minutes: Mapped[int] = mapped_column(
        Integer,
        default=60,
        nullable=False,
    )

    estimated_travel_minutes_before: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    estimated_distance_km_before: Mapped[float] = mapped_column(
        Float,
        default=0,
        nullable=False,
    )

    status: Mapped[PlanningAssignmentStatus] = mapped_column(
        SqlEnum(PlanningAssignmentStatus, name="planning_assignment_status"),
        default=PlanningAssignmentStatus.PLANNED,
        nullable=False,
        index=True,
    )

    source: Mapped[PlanningAssignmentSource] = mapped_column(
        SqlEnum(PlanningAssignmentSource, name="planning_assignment_source"),
        default=PlanningAssignmentSource.AI,
        nullable=False,
        index=True,
    )

    locked_by_planner: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
    )

    manual_override_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    planning_run: Mapped[PlanningRun] = relationship(
        "PlanningRun",
        back_populates="assignments",
    )

    branch: Mapped[Branch] = relationship(
        "Branch",
        back_populates="planning_assignments",
    )

    technician: Mapped[Technician] = relationship(
        "Technician",
        back_populates="planning_assignments",
    )

    ticket: Mapped[Ticket] = relationship(
        "Ticket",
        back_populates="planning_assignments",
    )

    def __repr__(self) -> str:
        return (
            f"PlanningAssignment(id={self.id!r}, "
            f"ticket_id={self.ticket_id!r}, "
            f"technician_id={self.technician_id!r}, "
            f"planned_start_at={self.planned_start_at!r}, "
            f"planned_end_at={self.planned_end_at!r})"
        )
