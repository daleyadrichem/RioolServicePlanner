from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .branch import Branch
    from .technician import Technician


class TechnicianAvailability(Base):
    """Planner-maintained per-day technician availability override."""

    __tablename__ = "technician_availability"
    __table_args__ = (
        UniqueConstraint(
            "branch_id",
            "technician_id",
            "available_date",
            name="uq_technician_availability_branch_technician_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

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

    available_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        index=True,
    )

    is_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
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

    branch: Mapped[Branch] = relationship("Branch")
    technician: Mapped[Technician] = relationship("Technician")
