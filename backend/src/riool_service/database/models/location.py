from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.base import Base

if TYPE_CHECKING:
    from .branch import Branch
    from .route_cache import RouteCache
    from .tickets import Ticket
    from .simulation_tickets import SimulationTicket
    from .technician import Technician

class Location(Base):
    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)

    input_address: Mapped[str] = mapped_column(String(500), nullable=False)

    formatted_address: Mapped[str] = mapped_column(
        String(500), nullable=True, unique=True
    )
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    house_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)

    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)

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
        back_populates="location",
    )

    simulation_tickets: Mapped[list[SimulationTicket]] = relationship(
        "SimulationTicket",
        back_populates="location",
    )

    branches: Mapped[list[Branch]] = relationship(
        "Branch",
        back_populates="location",
    )

    technician_homes: Mapped[list[Technician]] = relationship(
        "Technician",
        foreign_keys="Technician.home_location_id",
        back_populates="home_location",
    )

    routes_from: Mapped[list[RouteCache]] = relationship(
        "RouteCache",
        foreign_keys="RouteCache.from_location_id",
        back_populates="from_location",
    )

    routes_to: Mapped[list[RouteCache]] = relationship(
        "RouteCache",
        foreign_keys="RouteCache.to_location_id",
        back_populates="to_location",
    )

    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def osrm_coordinates(self) -> str:
        """
        OSRM verwacht: longitude,latitude
        Bijvoorbeeld: 5.3167,51.6978
        """
        if not self.has_coordinates():
            raise ValueError("Location has no coordinates")

        return f"{self.longitude},{self.latitude}"

    def __repr__(self) -> str:
        return (
            f"Location(id={self.id!r}, "
            f"address={self.formatted_address or self.input_address!r})"
        )
