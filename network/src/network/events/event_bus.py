"""In-process event bus for the Network Control Layer (Submodule 1.3).

Thread-safe pub/sub. Subscribers are callables taking a single
:class:`Event` argument. No external messaging (no Redis/Kafka/RabbitMQ).
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Callable, Dict

from network.events.event import Event

EventHandler = Callable[[Event], None]

_logger = logging.getLogger("network.events")


class EventBus:
    """Minimal synchronous in-process event bus."""

    def __init__(self):
        self._handlers: Dict[int, EventHandler] = {}
        self._ids = itertools.count(1)
        self._lock = threading.RLock()

    def subscribe(self, handler: EventHandler) -> int:
        """Register a handler; returns an unsubscribe token."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            token = next(self._ids)
            self._handlers[token] = handler
            return token

    def unsubscribe(self, token: int) -> bool:
        """Remove a handler by token. Returns True if removed."""
        with self._lock:
            return self._handlers.pop(token, None) is not None

    def publish(self, event: Event) -> int:
        """Deliver an event to every subscriber. Returns handler count.

        A failing subscriber never breaks delivery to the others; the
        exception is logged and swallowed.
        """
        with self._lock:
            handlers = list(self._handlers.values())
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - subscriber isolation
                _logger.exception("event handler failed for %s", event.type)
        return len(handlers)

    def clear(self) -> None:
        """Remove all subscribers."""
        with self._lock:
            self._handlers.clear()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._handlers)
