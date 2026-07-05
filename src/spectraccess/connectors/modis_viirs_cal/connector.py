from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pandas as pd

from spectraccess.connectors.thredds import ThreddsDataset, fetch_catalog
from spectraccess.core.connector import Connector
from spectraccess.core.fetch import fetch_url


@dataclass(frozen=True)
class VIIRSCatalog:
    source: str
    url: str | None


DEFAULT_VIIRS_CATALOG = VIIRSCatalog(
    "NOAA STAR VIIRS F-factors",
    # TODO: verify live THREDDS catalog URL before enabling as a default.
    None,
)


class VIIRSCalibrationConnector(Connector):
    """Connector for public VIIRS calibration LUT/F-factor products."""

    def __init__(self, catalog: VIIRSCatalog = DEFAULT_VIIRS_CATALOG, *, cache_dir: str | Path | None = None) -> None:
        self.catalog = catalog
        self.cache_dir = cache_dir

    def discover(self, **kwargs: object) -> list[ThreddsDataset]:
        if self.catalog.url is None:
            raise NotImplementedError(
                "STOPPED-AT-STUB: NOAA STAR VIIRS F-factor THREDDS catalog URL requires verification"
            )
        return fetch_catalog(self.catalog.url, source_agency="NOAA STAR", cache_dir=self.cache_dir, **kwargs)

    def fetch(self, target: ThreddsDataset | str, **kwargs: object) -> bytes:
        url = target.access_url if isinstance(target, ThreddsDataset) else target
        if not url:
            raise ValueError("target does not include an access URL")
        return fetch_url(url, cache_dir=self.cache_dir, **kwargs)

    def parse(self, raw: bytes | str) -> pd.DataFrame:
        text = _read_text(raw)
        table = pd.read_csv(StringIO(text), comment="#")
        return _normalize_viirs_table(table)


class MODISNotImplemented(Connector):
    """Documented MODIS MCST placeholder.

    MCST calibration LUT access is currently web-only in this scaffold and
    needs a scraping or manual-download connector design later.
    """

    def discover(self, **_kwargs: object) -> list[object]:
        raise NotImplementedError("MODIS MCST is web-only; connector design is pending")

    def fetch(self, target: object, **_kwargs: object) -> bytes:
        raise NotImplementedError("MODIS MCST is web-only; connector design is pending")

    def parse(self, raw: bytes | str) -> pd.DataFrame:
        return pd.read_csv(StringIO(_read_text(raw)), comment="#")


class ModisViirsCalConnector(VIIRSCalibrationConnector):
    """Backward-compatible umbrella name for the implemented VIIRS side."""


def _read_text(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    path = Path(raw)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return raw


def _normalize_viirs_table(table: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "timestamp": ["timestamp", "date", "time"],
        "sensor": ["sensor", "instrument"],
        "platform": ["platform", "satellite"],
        "band": ["band", "channel"],
        "f_factor": ["f_factor", "ffactor", "value"],
        "source_agency": ["source_agency", "agency"],
    }
    normalized = pd.DataFrame()
    lower_columns = {column.lower().strip(): column for column in table.columns}
    for output, candidates in aliases.items():
        for candidate in candidates:
            source = lower_columns.get(candidate)
            if source is not None:
                normalized[output] = table[source]
                break
    if "timestamp" in normalized:
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    if "source_agency" not in normalized:
        normalized["source_agency"] = "NOAA STAR"
    return normalized

