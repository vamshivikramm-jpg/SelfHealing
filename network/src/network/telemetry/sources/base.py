"""Telemetry source interface (Submodule 2.3).

A source turns one observation of the network into a list of
:class:`~network.telemetry.metrics.Sample` objects. The collector does
not care where samples come from, so a simulated source used for
development can later be swapped for (or run next to) live sources
such as OpenFlow port statistics from Ryu or ping/iperf probes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from network.telemetry.metrics import Sample


class TelemetrySource(ABC):
    """Abstract base class for every telemetry source."""

    #: Unique, human-readable source name (appears on every sample).
    name: str = "source"

    #: True if the values do not come from a real network.
    simulated: bool = False

    @abstractmethod
    def collect(self, timestamp: float) -> List[Sample]:
        """Observe the network once and return samples stamped ``timestamp``.

        Implementations may raise; the collector isolates failures so one
        broken source never stops the others.
        """

    def describe(self) -> dict:
        return {"name": self.name, "simulated": self.simulated, "class": type(self).__name__}
