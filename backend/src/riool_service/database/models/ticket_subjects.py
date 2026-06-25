from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .tickets import Ticket
    from .simulation_tickets import SimulationTicket

class TicketSubject(Base):
    __tablename__ = "ticket_subjects"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    estimated_duration_minutes: Mapped[int] = mapped_column(
        Integer,
        default=60,
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

    tickets: Mapped[list[Ticket]] = relationship(
        "Ticket",
        back_populates="subject",
    )

    simulation_tickets: Mapped[list[SimulationTicket]] = relationship(
        "SimulationTicket",
        back_populates="subject",
    )

    def __repr__(self) -> str:
        return (
            f"TicketSubject(id={self.id!r}, "
            f"name={self.name!r}, "
            f"estimated_duration_minutes={self.estimated_duration_minutes!r})"
        )
