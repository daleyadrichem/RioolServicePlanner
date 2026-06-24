"""Technician seed data."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from riool_service.database.models.branch import Branch
from riool_service.database.models.requirement import Requirement
from riool_service.database.models.technician import Technician, TechnicianStatus
from riool_service.database.models.technician_requirement import TechnicianRequirement


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
            select(Requirement).where(func.lower(Requirement.code) == str(code).lower())
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
