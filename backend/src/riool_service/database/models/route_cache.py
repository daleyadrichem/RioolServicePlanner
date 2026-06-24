from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from riool_service.database.models.tickets import Base

if TYPE_CHECKING:
    from .location import Location


class RouteProvider(str, Enum):
    OSRM = "OSRM"
    OPENROUTESERVICE = "OPENROUTESERVICE"
    MANUAL = "MANUAL"


class RouteCache(Base):
    __tablename__ = "route_cache"

    __table_args__ = (
        UniqueConstraint(
            "from_location_id",
            "to_location_id",
            "provider",
            name="uq_route_cache_from_to_provider",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    from_location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )

    to_location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id"),
        nullable=False,
        index=True,
    )

    provider: Mapped[RouteProvider] = mapped_column(
        SqlEnum(RouteProvider, name="route_provider"),
        default=RouteProvider.OSRM,
        nullable=False,
        index=True,
    )

    travel_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    distance_km: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    from_location: Mapped[Location] = relationship(
        "Location",
        foreign_keys=[from_location_id],
        back_populates="routes_from",
    )

    to_location: Mapped[Location] = relationship(
        "Location",
        foreign_keys=[to_location_id],
        back_populates="routes_to",
    )

    def __repr__(self) -> str:
        return (
            f"RouteCache(id={self.id!r}, "
            f"from_location_id={self.from_location_id!r}, "
            f"to_location_id={self.to_location_id!r}, "
            f"travel_minutes={self.travel_minutes!r}, "
            f"distance_km={self.distance_km!r})"
        )
