from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.tickets import Base

if TYPE_CHECKING:
    from .requirement import Requirement
    from .technician import Technician


class TechnicianRequirement(Base):
    __tablename__ = "technician_requirements"

    __table_args__ = (
        UniqueConstraint(
            "technician_id",
            "requirement_id",
            name="uq_technician_requirement",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    technician_id: Mapped[int] = mapped_column(
        ForeignKey("technicians.id"),
        nullable=False,
        index=True,
    )

    requirement_id: Mapped[int] = mapped_column(
        ForeignKey("requirements.id"),
        nullable=False,
        index=True,
    )

    technician: Mapped[Technician] = relationship(
        "Technician",
        back_populates="technician_requirements",
    )

    requirement: Mapped[Requirement] = relationship(
        "Requirement",
        back_populates="technician_requirements",
    )

    def __repr__(self) -> str:
        return (
            f"TechnicianRequirement("
            f"technician_id={self.technician_id!r}, "
            f"requirement_id={self.requirement_id!r})"
        )
