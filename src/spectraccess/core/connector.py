from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Connector(ABC):
    """Base class for source-specific spectral reference data connectors.

    Connectors should expose a three-stage flow:
    `discover()` returns available targets, `fetch()` retrieves raw bytes or a
    local file for one target, and `parse()` converts that raw payload into a
    tidy structure. Tidy outputs should prefer common names where possible:
    `timestamp`, `sensor`, `platform`, `band` or `channel`, `value`,
    `uncertainty` when available, and `source_agency` or `source_portal`.
    Outputs are usually `pandas.DataFrame` tables or `xarray.Dataset` objects.
    """

    @abstractmethod
    def discover(self, **kwargs: Any) -> Any:
        """Return source-specific targets available for fetching."""

    @abstractmethod
    def fetch(self, target: Any, **kwargs: Any) -> bytes | str:
        """Fetch a target and return raw bytes or a local file path."""

    @abstractmethod
    def parse(self, raw: bytes | str) -> Any:
        """Parse raw content into a tidy table or dataset."""

    def run(self, *, fetch_kwargs: dict[str, Any] | None = None, **discover_kwargs: Any) -> Any:
        """Discover the first target, fetch it, and parse it.

        Keyword arguments go to ``discover()`` only -- discovery filters like
        ``contains``/``max_catalogs`` are not valid fetch options. Pass
        fetch-stage options (``timeout``, ``use_cache``, ...) via
        ``fetch_kwargs``.
        """
        targets = self.discover(**discover_kwargs)
        if not targets:
            raise ValueError("discover() returned no targets")
        raw = self.fetch(targets[0], **(fetch_kwargs or {}))
        return self.parse(raw)

