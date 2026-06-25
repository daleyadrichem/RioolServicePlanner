from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .branch import Branch
    from .location import Location
    from .planning_assignment import PlanningAssignment
    from .technician_requirement import TechnicianRequirement


class TechnicianStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNAVAILABLE = "UNAVAILABLE"


class Technician(Base):
    __tablename__ = "technicians"

    id: Mapped[int] = mapped_column(primary_key=True)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    home_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id"),
        nullable=True,
        index=True,
    )

    status: Mapped[TechnicianStatus] = mapped_column(
        SqlEnum(TechnicianStatus, name="technician_status"),
        default=TechnicianStatus.ACTIVE,
        nullable=False,
        index=True,
    )

    workday_start_minutes: Mapped[int] = mapped_column(
        Integer,
        default=8 * 60,
        nullable=False,
    )

    workday_end_minutes: Mapped[int] = mapped_column(
        Integer,
        default=17 * 60,
        nullable=False,
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

    branch: Mapped[Branch] = relationship(
        "Branch",
        back_populates="technicians",
    )

    home_location: Mapped[Location | None] = relationship(
        "Location",
        foreign_keys=[home_location_id],
        back_populates="technician_homes",
    )

    @property
    def start_location(self) -> Location | None:
        """Return the technician start location for route planning.

        A technician starts from home when available. If no home location is
        configured, the branch location is used as a safe fallback.
        """
        if self.home_location is not None:
            return self.home_location
        if self.branch is not None:
            return self.branch.location
        return None

    @property
    def end_location(self) -> Location | None:
        """Return the technician end location for route planning."""
        return self.start_location

    technician_requirements: Mapped[list[TechnicianRequirement]] = relationship(
        "TechnicianRequirement",
        back_populates="technician",
        cascade="all, delete-orphan",
    )

    planning_assignments: Mapped[list[PlanningAssignment]] = relationship(
        "PlanningAssignment",
        back_populates="technician",
    )

    def __repr__(self) -> str:
        return (
            f"Technician(id={self.id!r}, "
            f"name={self.name!r}, "
            f"branch_id={self.branch_id!r}, "
            f"home_location_id={self.home_location_id!r}, "
            f"status={self.status!r})"
        )
