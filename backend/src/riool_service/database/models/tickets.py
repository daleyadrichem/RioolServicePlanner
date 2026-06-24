from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from .branch import Branch
    from .location import Location
    from .planning_assignment import PlanningAssignment
    from .ticket_requirement import TicketRequirement
    from .ticket_subjects import TicketSubject


class Base(DeclarativeBase):
    pass


class TicketUrgency(str, Enum):
    URGENT = "URGENT"  # binnen 8 uur
    MEDIUM = "MEDIUM"  # binnen 2 dagen
    LOW = "LOW"  # binnen 3 dagen


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Ticket(Base):
    __tablename__ = "tickets"

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
        back_populates="tickets",
    )

    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )

    location: Mapped[Location] = relationship(
        "Location",
        back_populates="tickets",
    )

    subject_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_subjects.id"),
        nullable=False,
        index=True,
    )

    subject: Mapped[TicketSubject] = relationship(
        "TicketSubject",
        back_populates="tickets",
    )

    ticket_requirements: Mapped[list[TicketRequirement]] = relationship(
        "TicketRequirement",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )

    planning_assignments: Mapped[list[PlanningAssignment]] = relationship(
        "PlanningAssignment",
        back_populates="ticket",
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

    status: Mapped[TicketStatus] = mapped_column(
        SqlEnum(TicketStatus, name="ticket_status"),
        default=TicketStatus.OPEN,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    actual_duration_minutes: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"Ticket(id={self.id!r}, "
            f"subject={self.subject.name if self.subject else None!r}, "
            f"urgency={self.urgency!r}, "
            f"status={self.status!r})"
        )
