"""Structured logging for the network package (Submodule 1.3).

Log lines are emitted as JSON objects, one per record:

    {"timestamp": "...", "level": "INFO", "logger": "network.control",
     "message": "Network Started", "event": "NETWORK_STARTED", "result": "success"}
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_configured = set()


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter with optional event/result fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event is not None:
            payload["event"] = event
        result = getattr(record, "result", None)
        if result is not None:
            payload["result"] = result
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str) -> logging.Logger:
    """Return a logger emitting structured JSON lines to stdout."""
    logger = logging.getLogger(name)
    if name not in _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _configured.add(name)
    return logger
