"""Compatibility entry point for the ticket simulator CLI.

Configuration via .env::

    # .env
    TICKET_SCENARIOS_CONFIG_PATH=scenarios.json

    python generated_ticket_simulator.py den_bosch_default

The ``--scenarios`` CLI option can still be used to override the .env value.
"""

from __future__ import annotations

from riool_service.simulator.ticket_simulator.cli import main


if __name__ == "__main__":
    main()
