from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.tickets import Base

if TYPE_CHECKING:
    from .location import Location
    from .planning_assignment import PlanningAssignment
    from .planning_run import PlanningRun
    from .technician import Technician
    from .tickets import Ticket


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )

    location: Mapped[Location] = relationship(
        "Location",
        back_populates="branches",
    )

    tickets: Mapped[list[Ticket]] = relationship(
        "Ticket",
        back_populates="branch",
        cascade="all, delete-orphan",
    )

    technicians: Mapped[list[Technician]] = relationship(
        "Technician",
        back_populates="branch",
        cascade="all, delete-orphan",
    )

    planning_runs: Mapped[list[PlanningRun]] = relationship(
        "PlanningRun",
        back_populates="branch",
    )

    planning_assignments: Mapped[list[PlanningAssignment]] = relationship(
        "PlanningAssignment",
        back_populates="branch",
    )

    def __repr__(self) -> str:
        return f"Branch(id={self.id!r}, name={self.name!r})"
