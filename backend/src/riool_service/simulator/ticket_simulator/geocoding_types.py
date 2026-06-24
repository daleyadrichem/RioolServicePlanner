"""Protocols for values returned by the geocoding service."""

from __future__ import annotations

from typing import Protocol


class ResolvedAddress(Protocol):
    """Address shape consumed by the ticket simulator."""

    status: str
    street: str
    house_number: str | int | None
    city: str
    country: str
