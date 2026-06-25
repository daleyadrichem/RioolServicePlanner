from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from riool_service.database.models.tickets import Base, TicketUrgency

if TYPE_CHECKING:
    from .branch import Branch
    from .location import Location
    from .ticket_requirement import TicketRequirement
    from .ticket_subjects import TicketSubject

class SimulationTicket(Base):
    __tablename__ = "simulation_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)

    ### --------------------------
    ### Relationships
    ### --------------------------
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id"),
        nullable=False,
        index=True,
    )

    branch: Mapped[Branch] = relationship(
        "Branch",
        back_populates="simulation_tickets",
    )

    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )

    location: Mapped[Location] = relationship(
        "Location",
        back_populates="simulation_tickets",
    )

    subject_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_subjects.id"),
        nullable=False,
        index=True,
    )

    subject: Mapped[TicketSubject] = relationship(
        "TicketSubject",
        back_populates="simulation_tickets",
    )

    ticket_requirements: Mapped[list[TicketRequirement]] = relationship(
        "TicketRequirement",
        back_populates="simulation_ticket",
        cascade="all, delete-orphan",
    )

    ### --------------------------
    ### Fields
    ### --------------------------
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    urgency: Mapped[TicketUrgency] = mapped_column(
        SqlEnum(TicketUrgency, name="ticket_urgency"),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"Ticket(id={self.id!r}, "
            f"subject={self.subject.name if self.subject else None!r}, "
            f"urgency={self.urgency!r}"
        )
