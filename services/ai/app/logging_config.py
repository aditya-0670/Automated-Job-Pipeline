"""Structured JSON logging with request correlation.

JSON because logs are read by machines in production (Part 20 ships Grafana);
a correlation id because tracing one resume run across nodes and services is
impossible without one.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from pythonjsonlogger import json as jsonlogger

#: Set per request by middleware, read by the log filter. A ContextVar rather
#: than a thread-local because the service is async -- concurrent requests share
#: threads, so a thread-local would leak ids between sessions.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        return True


def configure_logging(level: str = "INFO", *, service: str = "resumeforge-ai") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(session_id)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
            static_fields={"service": service},
        )
    )
    handler.addFilter(CorrelationFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours so every line
    # in the container is the same shape.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True
