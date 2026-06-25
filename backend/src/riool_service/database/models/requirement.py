from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .technician_requirement import TechnicianRequirement
    from .ticket_requirement import TicketRequirement


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(primary_key=True)

    code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)

    ticket_requirements: Mapped[list[TicketRequirement]] = relationship(
        "TicketRequirement",
        back_populates="requirement",
        cascade="all, delete-orphan",
    )

    technician_requirements: Mapped[list[TechnicianRequirement]] = relationship(
        "TechnicianRequirement",
        back_populates="requirement",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"Requirement(id={self.id!r}, code={self.code!r})"
