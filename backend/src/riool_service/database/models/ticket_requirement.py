from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .requirement import Requirement
    from .tickets import Ticket
    from .simulation_tickets import SimulationTicket

class TicketRequirement(Base):
    __tablename__ = "ticket_requirements"

    __table_args__ = (
        UniqueConstraint(
            "ticket_id",
            "requirement_id",
            name="uq_ticket_requirement",
        ),
        UniqueConstraint(
            "simulation_ticket_id",
            "requirement_id",
            name="uq_simulation_ticket_requirement",
        ),
        CheckConstraint(
            """
            (
                ticket_id IS NOT NULL
                AND simulation_ticket_id IS NULL
            )
            OR
            (
                ticket_id IS NULL
                AND simulation_ticket_id IS NOT NULL
            )
            """,
            name="ck_ticket_requirement_exactly_one_ticket_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)



    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id"),
        nullable=True,
        index=True,
    )

    ticket: Mapped[Ticket] = relationship(
        "Ticket",
        back_populates="ticket_requirements",
    )

    simulation_ticket_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_tickets.id"),
        nullable=True,
        index=True,
    )

    simulation_ticket: Mapped[list[SimulationTicket]] = relationship(
        "SimulationTicket",
        back_populates="ticket_requirements",
    )

    requirement_id: Mapped[int] = mapped_column(
        ForeignKey("requirements.id"),
        index=True,
    )

    requirement: Mapped[Requirement] = relationship(
        "Requirement",
        back_populates="ticket_requirements",
    )
