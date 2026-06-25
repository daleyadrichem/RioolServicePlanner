from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeedResult:
    """Small return object for simulator seed methods."""

    scenario_id: str
    table_name: str
    created_count: int
    created_ids: list[int] = field(default_factory=list)
    skipped_requirement_links: int = 0
    notes: list[str] = field(default_factory=list)
