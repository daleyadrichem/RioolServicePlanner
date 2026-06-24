"""Initialize the database schema and seed the Den Bosch branch.

The initializer creates the schema and seeds:
- branch location
- default ticket/technician requirements
- technicians and their capabilities from a JSON config file
- reusable simulated ticket locations from a JSON config file

Usage
-----
SQLite::

    export DATABASE_URL="sqlite:///./app.db"
    python initialize_database.py

PostgreSQL::

    export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/my_database"
    python initialize_database.py

Optional configs via .env::

    TECHNICIANS_CONFIG_PATH=technicians_config.json
    LOCATIONS_CONFIG_PATH=locations_config.json
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from riool_service.database_initializer.config import load_initializer_settings
from riool_service.database_initializer.database import (
    create_database_if_missing,
    create_schema,
)
from riool_service.database_initializer.schema_image import (
    generate_database_schema_image,
)
from riool_service.database_initializer.seed_data import seed_database


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
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
    return parser


def main() -> None:
    """Run database initialization."""
    args = build_parser().parse_args()
    database_url, technicians_config, locations_config = load_initializer_settings()

    try:
        create_database_if_missing(database_url)

        engine = create_engine(database_url, echo=False, future=True)
        create_schema(engine)
        if not args.skip_schema_image:
            schema_image = generate_database_schema_image(output_path=args.schema_image)
            print(f"Generated database schema image: {schema_image}")

        with Session(engine) as session:
            seed_database(
                session,
                technicians_config=technicians_config,
                locations_config=locations_config,
            )
            session.commit()

    except ProgrammingError as exc:
        raise SystemExit(f"Database initialization failed: {exc}") from exc

    print(f"Initialized database: {database_url}")


if __name__ == "__main__":
    main()
