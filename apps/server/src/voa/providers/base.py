"""Observation provider port (SPEC 5, CON-03).

A provider turns one raw input item into observation candidates. It never
decides risk state, never stamps server fields and never reads a wall clock.
"""

from typing import Protocol, runtime_checkable

from voa.contracts.observation import ObservationDraft


@runtime_checkable
class ObservationProvider(Protocol):
    def process(self, item: object) -> list[ObservationDraft]:
        """Return drafts for one item; raise ValueError for an invalid item."""
        ...
