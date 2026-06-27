from __future__ import annotations

import argparse
import logging
import os
import signal
import time

from riool_service.database.db_utils import session_scope
from riool_service.services.planning_ai.service import ensure_planning_ai_tables, planning_worker_tick

logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(signum, frame) -> None:  # noqa: ANN001
    global _stop_requested
    _stop_requested = True
    logger.info("Stop signal received; planning worker will exit after this tick.")


def run_worker(poll_interval_seconds: float = 2.0) -> None:
    """Run the incremental planning worker process.

    Start it next to the FastAPI process with:
        python -m riool_service.services.planning_ai.worker
    """
    ensure_planning_ai_tables()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    logger.info("Incremental planning worker started with %.2fs polling interval", poll_interval_seconds)
    logger.debug("Planning worker debug logging enabled; stop_requested=%s", _stop_requested)

    tick_number = 0
    while not _stop_requested:
        tick_number += 1
        tick_started_at = time.monotonic()
        logger.debug("Planning worker tick %s started", tick_number)
        try:
            with session_scope() as session:
                result = planning_worker_tick(session)
                elapsed_ms = (time.monotonic() - tick_started_at) * 1000
                if result.get("planned"):
                    logger.info(
                        "Planning worker tick %s planned ticket %s into planning run %s in %.1fms: %s",
                        tick_number,
                        result.get("ticket_id"),
                        result.get("planning_run_id"),
                        elapsed_ms,
                        result,
                    )
                else:
                    logger.debug("Planning worker tick %s finished in %.1fms: %s", tick_number, elapsed_ms, result)
        except Exception:
            logger.exception("Planning worker tick %s failed", tick_number)
        time.sleep(poll_interval_seconds)

    logger.info("Incremental planning worker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Riool Service incremental planning worker.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between worker ticks. Default: 2.0")
    parser.add_argument(
        "--log-level",
        default=os.getenv("PLANNING_WORKER_LOG_LEVEL", "DEBUG"),
        help="Python logging level for this worker. Default: DEBUG, or PLANNING_WORKER_LOG_LEVEL.",
    )
    args = parser.parse_args()
    log_level = getattr(logging, str(args.log_level).upper(), logging.DEBUG)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logger.debug("Planning worker logging configured at %s", logging.getLevelName(log_level))
    run_worker(poll_interval_seconds=args.poll_interval)


if __name__ == "__main__":
    main()
