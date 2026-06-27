from __future__ import annotations

import argparse
import logging
import signal
import time

from riool_service.database.db_utils import session_scope
from riool_service.services.simulator_service.service import ensure_simulator_tables, worker_tick

logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(signum, frame) -> None:  # noqa: ANN001
    global _stop_requested
    _stop_requested = True
    logger.info("Stop signal received; simulator worker will exit after this tick.")


def run_worker(poll_interval_seconds: float = 1.0) -> None:
    """Run the separate simulator worker process.

    Start it next to the FastAPI process with:
        python -m riool_service.services.simulator_service.worker
    """
    ensure_simulator_tables()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    logger.info("Simulator worker started with %.2fs polling interval", poll_interval_seconds)

    while not _stop_requested:
        try:
            with session_scope() as session:
                result = worker_tick(session)
                logger.info(
                    "Worker tick | status=%s | simulation_time=%s | advanced=%s | injected=%s | assignment_status_updates=%s",
                    result.get("status"),
                    result.get("current_simulation_time"),
                    result.get("advanced"),
                    result.get("injected_count"),
                    result.get("assignment_status_updates"),
                )
                if result.get("injected_count"):
                    logger.info(
                        "Transferred %s simulation ticket(s) into real tickets table at %s",
                        result["injected_count"],
                        result.get("current_simulation_time"),
                    )
        except Exception:
            logger.exception("Simulator worker tick failed")
        time.sleep(poll_interval_seconds)

    logger.info("Simulator worker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Riool Service simulator worker.")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between worker ticks. Default: 1.0")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_worker(poll_interval_seconds=args.poll_interval)


if __name__ == "__main__":
    main()
