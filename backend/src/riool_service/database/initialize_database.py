"""Initialize the database schema and seed the Den Bosch branch.

The initializer creates the schema and seeds:
- branch location
- default ticket/technician requirements
- technicians and their capabilities from a JSON config file

Usage
-----
SQLite::

    export DATABASE_URL="sqlite:///./app.db"
    python initialize_database.py

PostgreSQL::

    export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/my_database"
    python initialize_database.py

Optional technician config via .env::

    # .env
    TECHNICIANS_CONFIG=technicians_config.json

    python initialize_database.py
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from typing import Any, Final, cast
from xml.sax.saxutils import escape

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from riool_service.database.db_utils import get_database_url
from riool_service.database.models.branch import Branch
from riool_service.database.models.location import Location
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.technician import Technician, TechnicianStatus
from riool_service.database.models.technician_requirement import TechnicianRequirement
from riool_service.database.models.ticket_subjects import (
    TicketSubject as _TicketSubject,
)
from riool_service.database.models.ticket_requirement import (
    TicketRequirement as _TicketRequirement,
)
from riool_service.database.models.tickets import Base
from riool_service.database.models.tickets import Ticket as _Ticket

# Imported so SQLAlchemy registers these tables in Base.metadata.create_all().
from riool_service.database.models.planning_assignment import (  # noqa: F401
    PlanningAssignment as _PlanningAssignment,
)
from riool_service.database.models.planning_run import PlanningRun as _PlanningRun  # noqa: F401
from riool_service.database.models.route_cache import RouteCache as _RouteCache  # noqa: F401

from riool_service.geocode_service import coordinates_from_address

_IMPORTED_MODELS: Final[tuple[type[Any], ...]] = (
    _Ticket,
    _TicketRequirement,
    _TicketSubject,
    Technician,
    TechnicianRequirement,
    _PlanningAssignment,
    _PlanningRun,
    _RouteCache,
)

ORIGINAL_LOCATION: Final[dict[str, str]] = {
    "input_address": "Jac. van Looystraat 5, 5216 SB 's-Hertogenbosch, NL",
    "formatted_address": "Jac. van Looystraat 5, 5216 SB 's-Hertogenbosch, Netherlands",
    "street": "Jac. van Looystraat",
    "house_number": "5",
    "city": "'s-Hertogenbosch",
    "country": "NL",
}

ORIGINAL_BRANCH_NAME: Final[str] = "Branch Den Bosch"

DEFAULT_REQUIREMENTS: Final[tuple[dict[str, str], ...]] = (
    {"code": "VEER", "name": "Trekveer"},
    {"code": "LADDER", "name": "Ladder"},
)

TECHNICIANS_CONFIG_ENV_VAR: Final[str] = "TECHNICIANS_CONFIG_PATH"
DEFAULT_TECHNICIANS_CONFIG_PATH: Final[str] = "technicians_config.json"


def create_database_if_missing(database_url: str) -> None:
    """Create the physical database when supported."""
    url = make_url(database_url)

    if url.drivername.startswith("sqlite"):
        if url.database and url.database not in {":memory:", ""}:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return

    if not url.drivername.startswith("postgresql"):
        return

    database_name = url.database
    if not database_name:
        return

    maintenance_url = url.set(database="postgres")
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT", future=True)

    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            ).scalar_one_or_none()

            if exists is None:
                safe_database_name = database_name.replace('"', '""')
                connection.execute(text(f'CREATE DATABASE "{safe_database_name}"'))
    finally:
        engine.dispose()


def create_schema(engine: Engine) -> None:
    """Create all database tables from SQLAlchemy metadata."""
    Base.metadata.create_all(engine)


def generate_database_schema_image(
    *,
    output_path: str | Path = "database_schema.svg",
) -> Path:
    """Generate an SVG ER-style image from the SQLAlchemy metadata.

    The image is generated from ``Base.metadata`` so it stays in sync with the
    registered SQLAlchemy models. It does not require a live database connection
    and has no external dependencies such as Graphviz.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    tables = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    if not tables:
        raise RuntimeError("No tables were registered in Base.metadata.")

    box_width = 310
    header_height = 34
    row_height = 22
    box_gap_x = 90
    box_gap_y = 70
    columns_per_row = 3
    margin = 40

    table_layout: dict[str, dict[str, int]] = {}
    row_heights: dict[int, int] = {}

    for index, table in enumerate(tables):
        row = index // columns_per_row
        height = header_height + row_height * max(1, len(table.columns)) + 14
        row_heights[row] = max(row_heights.get(row, 0), height)

    row_y: dict[int, int] = {}
    current_y = margin
    for row in range((len(tables) + columns_per_row - 1) // columns_per_row):
        row_y[row] = current_y
        current_y += row_heights[row] + box_gap_y

    for index, table in enumerate(tables):
        row = index // columns_per_row
        col = index % columns_per_row
        x = margin + col * (box_width + box_gap_x)
        y = row_y[row]
        height = header_height + row_height * max(1, len(table.columns)) + 14
        table_layout[table.name] = {
            "x": x,
            "y": y,
            "width": box_width,
            "height": height,
        }

    svg_width = (
        margin * 2 + columns_per_row * box_width + (columns_per_row - 1) * box_gap_x
    )
    svg_height = current_y + margin

    def column_label(column: Any) -> str:
        markers: list[str] = []
        if column.primary_key:
            markers.append("PK")
        if column.foreign_keys:
            markers.append("FK")

        prefix = f"[{', '.join(markers)}] " if markers else ""
        nullable = "" if column.nullable or column.primary_key else " NOT NULL"
        return f"{prefix}{column.name}: {column.type}{nullable}"

    def table_anchor(table_name: str, side: str) -> tuple[int, int]:
        box = table_layout[table_name]
        x = box["x"] if side == "left" else box["x"] + box["width"]
        y = box["y"] + box["height"] // 2
        return x, y

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#475569" />',
        "</marker>",
        "</defs>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="40" y="26" font-family="Arial, sans-serif" font-size="18" font-weight="700" fill="#0f172a">Database schema</text>',
    ]

    # Draw foreign-key relationships first, behind the table cards.
    for table in tables:
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                source_name = table.name
                target_name = foreign_key.column.table.name
                if source_name not in table_layout or target_name not in table_layout:
                    continue

                source_box = table_layout[source_name]
                target_box = table_layout[target_name]
                source_side = "left" if target_box["x"] < source_box["x"] else "right"
                target_side = "right" if source_side == "left" else "left"
                x1, y1 = table_anchor(source_name, source_side)
                x2, y2 = table_anchor(target_name, target_side)
                mid_x = (x1 + x2) // 2

                parts.append(
                    "<path "
                    f'd="M{x1},{y1} C{mid_x},{y1} {mid_x},{y2} {x2},{y2}" '
                    'fill="none" stroke="#475569" stroke-width="1.6" '
                    'marker-end="url(#arrow)" opacity="0.75"/>'
                )

    for table in tables:
        box = table_layout[table.name]
        x = box["x"]
        y = box["y"]
        width = box["width"]
        height = box["height"]

        parts.extend(
            [
                f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" fill="#ffffff" stroke="#cbd5e1"/>',
                f'<rect x="{x}" y="{y}" width="{width}" height="{header_height}" rx="10" fill="#1e293b"/>',
                f'<path d="M{x},{y + header_height - 10} H{x + width} V{y + header_height} H{x} Z" fill="#1e293b"/>',
                f'<text x="{x + 14}" y="{y + 23}" font-family="Arial, sans-serif" font-size="14" font-weight="700" fill="#ffffff">{escape(table.name)}</text>',
            ]
        )

        columns = list(table.columns)
        if not columns:
            columns = []
            parts.append(
                f'<text x="{x + 14}" y="{y + header_height + 24}" font-family="Arial, sans-serif" font-size="12" fill="#64748b">No columns</text>'
            )
        else:
            for index, column in enumerate(columns):
                text_y = y + header_height + 22 + index * row_height
                label = escape(column_label(column))
                font_weight = "700" if column.primary_key else "400"
                fill = "#0f172a" if column.primary_key else "#334155"
                parts.append(
                    f'<text x="{x + 14}" y="{text_y}" font-family="Arial, sans-serif" font-size="12" font-weight="{font_weight}" fill="{fill}">{label}</text>'
                )

    parts.append("</svg>")
    output.write_text("\n".join(parts), encoding="utf-8")
    return output


def seed_default_requirements(session: Session) -> None:
    """Insert or update the default requirements."""
    for requirement_data in DEFAULT_REQUIREMENTS:
        requirement = session.scalar(
            select(Requirement).where(Requirement.code == requirement_data["code"])
        )

        if requirement is None:
            session.add(Requirement(**requirement_data))
        else:
            requirement.name = requirement_data["name"]


def seed_original_branch(session: Session) -> Branch:
    """Insert or update the original Den Bosch branch."""
    location = session.scalar(
        select(Location).where(
            Location.street == ORIGINAL_LOCATION["street"],
            Location.house_number == ORIGINAL_LOCATION["house_number"],
            Location.city == ORIGINAL_LOCATION["city"],
        )
    )

    if location is None:
        coordinates = coordinates_from_address(
            ORIGINAL_LOCATION["street"],
            ORIGINAL_LOCATION["house_number"],
            ORIGINAL_LOCATION["city"],
            ORIGINAL_LOCATION["country"],
        )
        location = Location(
            longitude=coordinates.longitude,
            latitude=coordinates.latitude,
            **ORIGINAL_LOCATION,
        )
        session.add(location)
        session.flush()
    else:
        _fill_missing_location_fields(location)

    if location.id is None:
        session.flush()

    if location.id is None:
        raise RuntimeError("Location ID was not assigned after flushing the session.")

    branch = session.scalar(select(Branch).where(Branch.name == ORIGINAL_BRANCH_NAME))

    if branch is None:
        branch = Branch(
            name=ORIGINAL_BRANCH_NAME,
            location_id=location.id,
        )
        session.add(branch)
    else:
        branch.location_id = location.id

    session.flush()
    print(f"Seeded branch: {branch.name} at {location.formatted_address}")
    return branch


def load_technicians_config(path: str | Path) -> dict[str, Any]:
    """Load technician seed config."""
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(f"Technician config {config_path} was not found.")

    with config_path.open(encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            f"Technician config {config_path} must contain a JSON object at the top level."
        )

    return cast(dict[str, Any], data)


def seed_technicians(session: Session, config: dict[str, Any]) -> None:
    """Insert or update technicians and their capability requirements."""
    for branch_config in config.get("branches", []):
        branch_name = branch_config["branch_name"]
        branch = session.scalar(select(Branch).where(Branch.name == branch_name))
        if branch is None:
            raise ValueError(
                f"Branch {branch_name!r} was not found for technician seed"
            )

        for technician_data in branch_config.get("technicians", []):
            technician = session.scalar(
                select(Technician).where(
                    Technician.branch_id == branch.id,
                    Technician.name == technician_data["name"],
                )
            )

            status_value = technician_data.get("status", TechnicianStatus.ACTIVE.value)
            status = TechnicianStatus(status_value)
            workday_start = _time_to_minutes(
                technician_data.get("workday_start", "08:00")
            )
            workday_end = _time_to_minutes(technician_data.get("workday_end", "17:00"))

            if technician is None:
                technician = Technician(
                    branch_id=branch.id,
                    name=technician_data["name"],
                    status=status,
                    workday_start_minutes=workday_start,
                    workday_end_minutes=workday_end,
                )
                session.add(technician)
                session.flush()
            else:
                technician.status = status
                technician.workday_start_minutes = workday_start
                technician.workday_end_minutes = workday_end

            _replace_technician_requirements(
                session=session,
                technician=technician,
                requirement_codes=technician_data.get("requirements", []),
            )

            print(
                f"Seeded technician: {technician.name} "
                f"({', '.join(technician_data.get('requirements', [])) or 'no requirements'})"
            )


def seed_database(session: Session, technicians_config: dict[str, Any]) -> None:
    """Seed initial database records."""
    seed_default_requirements(session)
    seed_original_branch(session)
    seed_technicians(session, technicians_config)


def _replace_technician_requirements(
    *,
    session: Session,
    technician: Technician,
    requirement_codes: list[str],
) -> None:
    session.query(TechnicianRequirement).filter(
        TechnicianRequirement.technician_id == technician.id
    ).delete(synchronize_session=False)

    for code in requirement_codes:
        requirement = session.scalar(
            select(Requirement).where(Requirement.code == str(code).upper())
        )
        if requirement is None:
            raise ValueError(f"Requirement code {code!r} was not found")

        session.add(
            TechnicianRequirement(
                technician_id=technician.id,
                requirement_id=requirement.id,
            )
        )


def _time_to_minutes(value: str) -> int:
    hours, minutes = map(int, value.split(":"))
    if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
        raise ValueError(f"Invalid time value {value!r}")
    return hours * 60 + minutes


def _fill_missing_location_fields(location: Location) -> None:
    for field, value in ORIGINAL_LOCATION.items():
        current_value = getattr(location, field)

        if current_value in {None, ""}:
            setattr(location, field, value)


def main() -> None:
    """Run database initialization."""
    parser = argparse.ArgumentParser(description="Initialize and seed the database.")
    parser.add_argument(
        "--schema-image",
        default="database_schema.svg",
        help="Path for the generated database schema image. Defaults to database_schema.svg.",
    )
    parser.add_argument(
        "--skip-schema-image",
        action="store_true",
        help="Do not generate the database schema image.",
    )
    args = parser.parse_args()

    load_dotenv()

    database_url = get_database_url()
    technicians_config_path = os.getenv(
        TECHNICIANS_CONFIG_ENV_VAR,
        DEFAULT_TECHNICIANS_CONFIG_PATH,
    )
    technicians_config = load_technicians_config(technicians_config_path)

    try:
        create_database_if_missing(database_url)

        engine = create_engine(database_url, echo=False, future=True)
        create_schema(engine)
        if not args.skip_schema_image:
            schema_image = generate_database_schema_image(output_path=args.schema_image)
            print(f"Generated database schema image: {schema_image}")

        with Session(engine) as session:
            seed_database(session, technicians_config)
            session.commit()

    except ProgrammingError as exc:
        raise SystemExit(f"Database initialization failed: {exc}") from exc

    print(f"Initialized database: {database_url}")


if __name__ == "__main__":
    main()
