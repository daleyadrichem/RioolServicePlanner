"""Structured debug logging for the Riool Service backend.

The logger writes newline-delimited JSON records to ``RioolService.json`` by
default. The filename intentionally follows the project request, even though the
file is technically JSONL so it can be appended safely while the service runs.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request

LOG_FILE_ENV_VAR = "RIOOL_SERVICE_LOG_FILE"
DEFAULT_LOG_FILE = "RioolService.json"
_MAX_BODY_CHARS = 50_000
_MAX_REPR_CHARS = 10_000
_MAX_STACK_FRAMES = 80

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "riool_request_id",
    default=None,
)
request_context_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "riool_request_context",
    default={},
)

_logger: logging.Logger | None = None
_logger_lock = threading.Lock()


def _json_default(value: Any) -> str:
    """Return a JSON-safe representation for otherwise unserialisable values."""
    try:
        return repr(value)
    except Exception:  # pragma: no cover - defensive fallback
        return f"<unrepresentable {type(value).__name__}>"


def _truncate(value: str, max_chars: int = _MAX_REPR_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}...<truncated {len(value) - max_chars} chars>"


def safe_repr(value: Any, max_chars: int = _MAX_REPR_CHARS) -> str:
    """Return a bounded repr that will not break logging."""
    try:
        return _truncate(repr(value), max_chars=max_chars)
    except Exception:  # pragma: no cover - defensive fallback
        return f"<unrepresentable {type(value).__name__}>"


class JsonLineFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "json_payload", None)
        if not isinstance(payload, dict):
            payload = {"event": record.getMessage()}

        payload.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )
        payload.setdefault("level", record.levelname)
        payload.setdefault("logger", record.name)
        payload.setdefault("thread", threading.current_thread().name)

        request_id = request_id_var.get()
        if request_id:
            payload.setdefault("request_id", request_id)

        request_context = request_context_var.get()
        if request_context:
            payload.setdefault("request", request_context)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=_json_default, ensure_ascii=False)


def get_log_file_path() -> Path:
    """Return the configured debug log path."""
    return Path(os.getenv(LOG_FILE_ENV_VAR, DEFAULT_LOG_FILE)).expanduser()


def configure_debug_logging() -> logging.Logger:
    """Configure and return the shared Riool Service debug logger."""
    global _logger

    with _logger_lock:
        if _logger is not None:
            return _logger

        log_path = get_log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("riool_service.debug")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # Avoid duplicate file handlers during reloads/tests.
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(JsonLineFormatter())
        logger.addHandler(handler)

        _logger = logger
        return logger


def current_stack(skip: int = 1) -> list[str]:
    """Return the current stack as formatted frames.

    ``skip`` removes this helper and direct logging utility frames so the caller
    stack is easier to read in the log file.
    """
    stack = traceback.format_stack(limit=_MAX_STACK_FRAMES)
    if skip > 0:
        stack = stack[:-skip]
    return stack


def log_debug_event(event: str, **fields: Any) -> None:
    """Write one structured debug event to ``RioolService.json``."""
    logger = configure_debug_logging()
    payload: dict[str, Any] = {"event": event, **fields}
    logger.debug(event, extra={"json_payload": payload})


def log_exception_event(event: str, exc: BaseException, **fields: Any) -> None:
    """Write one structured exception event to ``RioolService.json``."""
    logger = configure_debug_logging()
    payload: dict[str, Any] = {
        "event": event,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        **fields,
    }
    logger.exception(event, extra={"json_payload": payload})


async def read_request_body_for_logging(request: Request) -> tuple[bytes, bool]:
    """Read and return the request body without consuming it for FastAPI.

    Starlette caches ``request.body()``, but replacing ``_receive`` keeps the
    body available for downstream handlers in older Starlette/FastAPI versions.
    """
    body = await request.body()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive  # noqa: SLF001 - intentional middleware hook
    return body, len(body.decode("utf-8", errors="replace")) > _MAX_BODY_CHARS


def body_to_log_text(body: bytes) -> str | None:
    """Return a bounded text representation of an HTTP body."""
    if not body:
        return None
    return _truncate(body.decode("utf-8", errors="replace"), _MAX_BODY_CHARS)


def start_request_context(request: Request, body: bytes) -> tuple[Any, Any, float]:
    """Create request correlation context and log API request start."""
    request_id = str(uuid.uuid4())
    request_id_token = request_id_var.set(request_id)

    context = {
        "method": request.method,
        "path": request.url.path,
        "query_string": request.url.query,
        "client": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }
    request_context_token = request_context_var.set(context)
    started_at = time.perf_counter()

    log_debug_event(
        "api.request.start",
        request_id=request_id,
        http={
            **context,
            "headers": dict(request.headers),
            "body": body_to_log_text(body),
            "body_bytes": len(body),
        },
        stack=current_stack(skip=2),
    )
    return request_id_token, request_context_token, started_at


def finish_request_context(
    *,
    response_status_code: int | None,
    started_at: float,
    error: BaseException | None = None,
) -> None:
    """Log API request completion or failure."""
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 3)
    payload: dict[str, Any] = {
        "status_code": response_status_code,
        "elapsed_ms": elapsed_ms,
        "stack": current_stack(skip=2),
    }
    if error is None:
        log_debug_event("api.request.finish", **payload)
    else:
        log_exception_event("api.request.error", error, **payload)
