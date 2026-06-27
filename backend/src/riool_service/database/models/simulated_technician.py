from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .planning_assignment import PlanningAssignment
    from .technician import Technician


class SimulatedTechnicianState(Base):
    """Frontend-controlled simulator state for a technician.

    The planner remains the source of truth for the route. This table only stores
    the currently relevant planning assignment row, the simulated end time
    chosen in the simulator UI, and whether that time applies to driving
    toward the ticket or to the ticket work itself.
    """

    __tablename__ = "simulated_technician_states"
    __table_args__ = (
        UniqueConstraint("technician_id", name="uq_simulated_technician_state_technician"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    technician_id: Mapped[int] = mapped_column(ForeignKey("technicians.id"), nullable=False, index=True)
    planning_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("planning_assignments.id"), nullable=True, index=True
    )
    simulated_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    simulated_time_applies_to: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    technician: Mapped[Technician] = relationship("Technician")
    planning_assignment: Mapped[PlanningAssignment | None] = relationship("PlanningAssignment")
