from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any, Sequence, TypeVar

from sqlalchemy import select

from riool_service.database.models.tickets import TicketUrgency

T = TypeVar("T")

URGENCY_DEADLINE_HOURS = {
    TicketUrgency.URGENT: 8,
    TicketUrgency.MEDIUM: 48,
    TicketUrgency.LOW: 72,
}


def clear_model_table(session: Any, model: type[Any]) -> None:
    """Clear a model table before refilling it.

    Direct dependent rows, such as ticket requirement links, are removed first
    when they reference rows from the table being cleared.
    """
    table = model.__table__
    primary_key_columns = list(table.primary_key.columns)
    if len(primary_key_columns) != 1:
        raise ValueError(f"clear_model_table expects {table.name} to have exactly one primary key")

    primary_key = primary_key_columns[0]

    for dependent_table in reversed(table.metadata.sorted_tables):
        if dependent_table is table:
            continue

        referencing_columns = [
            foreign_key.parent
            for foreign_key in dependent_table.foreign_keys
            if foreign_key.column.table is table
        ]
        for referencing_column in referencing_columns:
            session.execute(
                dependent_table.delete().where(
                    referencing_column.in_(select(primary_key))
                )
            )

    session.execute(table.delete())
    session.flush()




def clear_rows_by_description_marker(session: Any, model: type[Any], marker: str) -> int:
    """Delete only rows whose description contains a simulator marker.

    This protects manually created tickets from being removed when the
    simulator is regenerated or cleared. Dependent rows are removed first so
    this works even when the database does not have ON DELETE CASCADE.
    """
    table = model.__table__
    primary_key_columns = list(table.primary_key.columns)
    if len(primary_key_columns) != 1:
        raise ValueError(f"clear_rows_by_description_marker expects {table.name} to have exactly one primary key")
    if "description" not in table.c:
        raise ValueError(f"{table.name} does not have a description column")

    primary_key = primary_key_columns[0]
    ids = [
        row[0]
        for row in session.execute(
            select(primary_key).where(table.c.description.contains(marker))
        ).all()
    ]
    if not ids:
        return 0

    for dependent_table in reversed(table.metadata.sorted_tables):
        if dependent_table is table:
            continue

        referencing_columns = [
            foreign_key.parent
            for foreign_key in dependent_table.foreign_keys
            if foreign_key.column.table is table
        ]
        for referencing_column in referencing_columns:
            session.execute(dependent_table.delete().where(referencing_column.in_(ids)))

    session.execute(table.delete().where(primary_key.in_(ids)))
    session.flush()
    return len(ids)


def make_rng(seed: int | None = None) -> random.Random:
    """Create a local RNG so simulations can be reproducible."""
    return random.Random(seed)


def parse_time(value: str) -> time:
    hours, minutes = value.split(":", maxsplit=1)
    return time(hour=int(hours), minute=int(minutes))


def combine_day_and_time(day: date, value: str) -> datetime:
    return datetime.combine(day, parse_time(value))


def random_datetime_between(
    rng: random.Random,
    start_at: datetime,
    end_at: datetime,
) -> datetime:
    if end_at <= start_at:
        return start_at
    seconds = int((end_at - start_at).total_seconds())
    return start_at + timedelta(seconds=rng.randint(0, seconds))


def weighted_choice(rng: random.Random, weighted_items: Sequence[tuple[T, int]]) -> T:
    """Pick one value from ``[(value, weight), ...]``."""
    items = [(item, max(0, int(weight))) for item, weight in weighted_items]
    total = sum(weight for _, weight in items)
    if total <= 0:
        raise ValueError("At least one weight must be greater than zero")

    marker = rng.uniform(0, total)
    upto = 0.0
    for item, weight in items:
        upto += weight
        if marker <= upto:
            return item
    return items[-1][0]


def choose_urgency(
    rng: random.Random,
    urgent_percentage: int,
    medium_percentage: int,
    low_percentage: int,
) -> TicketUrgency:
    return weighted_choice(
        rng,
        [
            (TicketUrgency.URGENT, urgent_percentage),
            (TicketUrgency.MEDIUM, medium_percentage),
            (TicketUrgency.LOW, low_percentage),
        ],
    )


def deadline_for(created_at: datetime, urgency: TicketUrgency) -> datetime:
    return created_at + timedelta(hours=URGENCY_DEADLINE_HOURS[urgency])


def maybe_requirement_codes(
    rng: random.Random,
    requirements_percentages: dict[str, int],
) -> list[str]:
    """Return requirement codes that should be attached to a generated ticket."""
    selected: list[str] = []
    for requirement_code, percentage in requirements_percentages.items():
        if rng.randint(1, 100) <= int(percentage):
            selected.append(requirement_code)
    return selected


def haversine_km(
    lat_a: float,
    lon_a: float,
    lat_b: float,
    lon_b: float,
) -> float:
    """Distance in kilometers between two WGS84 points."""
    earth_radius_km = 6371.0
    d_lat = radians(lat_b - lat_a)
    d_lon = radians(lon_b - lon_a)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat_a)) * cos(radians(lat_b)) * sin(d_lon / 2) ** 2
    )
    return 2 * earth_radius_km * asin(sqrt(a))
