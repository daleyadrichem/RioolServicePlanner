"""Ticket simulator package.

This package contains only write-side simulator functionality for now:

- ``seed_tickets`` writes tickets directly into the production ``tickets`` table.
- ``seed_simulation_tickets`` prepares tickets in ``simulation_tickets`` with a
  planned ``created_at`` timestamp so they can later be released throughout the day.
"""

from .fill_simulation_tickets import seed_simulation_tickets
from .fill_tickets import seed_tickets

__all__ = ["seed_tickets", "seed_simulation_tickets"]
