"""Import SQLAlchemy models that must be registered for relationship resolution."""

from __future__ import annotations

# These imports intentionally register model classes with SQLAlchemy metadata.
from riool_service.database.models.planning_assignment import (
    PlanningAssignment as PlanningAssignment,
)
from riool_service.database.models.planning_run import PlanningRun as PlanningRun
from riool_service.database.models.route_cache import RouteCache as RouteCache
from riool_service.database.models.technician import Technician as Technician
from riool_service.database.models.technician_requirement import (
    TechnicianRequirement as TechnicianRequirement,
)

__all__ = [
    "PlanningAssignment",
    "PlanningRun",
    "RouteCache",
    "Technician",
    "TechnicianRequirement",
]
