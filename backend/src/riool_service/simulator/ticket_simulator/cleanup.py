"""Database cleanup helpers for generated ticket data."""

from __future__ import annotations

from sqlalchemy.orm import Session

from riool_service.database.models.ticket_requirement import TicketRequirement
from riool_service.database.models.tickets import Ticket


def clear_existing_tickets(session: Session) -> None:
    """Remove existing ticket input data without touching planner or technician tables."""
    session.query(TicketRequirement).delete(synchronize_session=False)
    session.query(Ticket).delete(synchronize_session=False)
    session.flush()
