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

    def parse_canonical(self, raw: bytes | str, **kwargs: Any) -> Any:
        """Parse raw content into the canonical tidy schema (`core.schema`).

        Connectors that emit the canonical schema override this. The default
        raises so a missing implementation is loud, never a silent gap: every
        connector is expected to grow a canonical emitter as the schema rolls
        out (schema v1 ships with GSICS as the first emitter).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement the canonical schema yet; "
            "use parse() for its native output"
        )

    def _parse_kwargs_for(self, target: Any) -> dict[str, Any]:
        """Provenance kwargs a discovered ``target`` contributes to ``parse()``.

        The convenience path (``run()``) discovers a target, fetches it, then
        parses -- but ``parse()`` takes only raw bytes, so any provenance the
        target carried (source agency, access URL, ...) is lost unless the
        connector maps it forward here. Default: nothing target-derived.
        Connectors whose ``parse()`` accepts provenance keywords override this
        so ``run()`` stays lossless. The returned keys MUST be accepted by the
        connector's own ``parse()`` signature.
        """
        return {}

    def _canonical_kwargs_for(self, target: Any) -> dict[str, Any]:
        """Provenance kwargs a target contributes to ``parse_canonical()``.

        Same role as :meth:`_parse_kwargs_for` but for the canonical path,
        whose provenance keys differ (canonical output carries ``source_url``
        and ``retrieved_at`` that native ``parse()`` has no column for). The
        returned keys MUST be accepted by the connector's ``parse_canonical()``.
        """
        return {}

    def run(
        self,
        *,
        fetch_kwargs: dict[str, Any] | None = None,
        canonical: bool = False,
        parse_kwargs: dict[str, Any] | None = None,
        **discover_kwargs: Any,
    ) -> Any:
        """Discover the first target, fetch it, and parse it.

        Keyword arguments go to ``discover()`` only -- discovery filters like
        ``contains``/``max_catalogs`` are not valid fetch options. Pass
        fetch-stage options (``timeout``, ``use_cache``, ...) via
        ``fetch_kwargs``.

        The discovered target's provenance is carried into the parse stage via
        :meth:`_parse_kwargs_for` / :meth:`_canonical_kwargs_for`, so the
        convenience path keeps the same provenance a manual
        ``discover -> fetch -> parse`` would. Set ``canonical=True`` to emit the
        canonical schema (``parse_canonical``) instead of native ``parse``.
        ``parse_kwargs`` is merged last, so a caller can add or override a
        provenance key (e.g. ``retrieved_at``).
        """
        targets = self.discover(**discover_kwargs)
        if not targets:
            raise ValueError("discover() returned no targets")
        target = targets[0]
        raw = self.fetch(target, **(fetch_kwargs or {}))
        if canonical:
            kwargs = {**self._canonical_kwargs_for(target), **(parse_kwargs or {})}
            return self.parse_canonical(raw, **kwargs)
        kwargs = {**self._parse_kwargs_for(target), **(parse_kwargs or {})}
        return self.parse(raw, **kwargs)

