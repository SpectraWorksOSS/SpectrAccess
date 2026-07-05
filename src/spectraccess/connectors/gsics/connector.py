from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd

from spectraccess.connectors.thredds import ThreddsDataset, fetch_catalog
from spectraccess.core.connector import Connector
from spectraccess.core.fetch import fetch_url


@dataclass(frozen=True)
class GSICSCatalog:
    agency: str
    url: str | None


DEFAULT_CATALOGS: tuple[GSICSCatalog, ...] = (
    # TODO: verify live THREDDS catalog URL before enabling as a default.
    GSICSCatalog("EUMETSAT", None),
    # TODO: verify live THREDDS catalog URL before enabling as a default.
    GSICSCatalog("NOAA STAR", None),
    # TODO: verify live THREDDS catalog URL before enabling as a default.
    GSICSCatalog("CMA", None),
)


class GSICSConnector(Connector):
    """Connector for GSICS GPPA correction coefficient products."""

    def __init__(self, catalogs: Iterable[GSICSCatalog] | None = None, *, cache_dir: str | Path | None = None) -> None:
        self.catalogs = tuple(catalogs or DEFAULT_CATALOGS)
        self.cache_dir = cache_dir

    def discover(self, **kwargs: object) -> list[ThreddsDataset]:
        targets: list[ThreddsDataset] = []
        missing = [catalog.agency for catalog in self.catalogs if catalog.url is None]
        if missing and len(missing) == len(self.catalogs):
            raise NotImplementedError(
                "STOPPED-AT-STUB: GSICS GPPA live THREDDS catalog URLs require verification "
                f"for {', '.join(missing)}"
            )
        for catalog in self.catalogs:
            if catalog.url is None:
                continue
            targets.extend(fetch_catalog(catalog.url, source_agency=catalog.agency, cache_dir=self.cache_dir, **kwargs))
        return targets

    def fetch(self, target: ThreddsDataset | str, **kwargs: object) -> bytes:
        url = target.access_url if isinstance(target, ThreddsDataset) else target
        if not url:
            raise ValueError("target does not include an access URL")
        return fetch_url(url, cache_dir=self.cache_dir, **kwargs)

    def parse(self, raw: bytes | str) -> pd.DataFrame:
        payload = _read_payload(raw)
        table = _read_table(payload)
        return _normalize_gsics_table(table)


def _read_payload(raw: bytes | str) -> bytes:
    if isinstance(raw, bytes):
        return raw
    path = Path(raw)
    if path.exists():
        return path.read_bytes()
    return raw.encode("utf-8")


def _read_table(payload: bytes) -> pd.DataFrame:
    text = payload.decode("utf-8", errors="replace")
    if "," in text.splitlines()[0]:
        return pd.read_csv(StringIO(text), comment="#")
    try:
        return pd.read_csv(BytesIO(payload), sep=None, engine="python", comment="#")
    except Exception:
        return pd.read_csv(StringIO(text), delim_whitespace=True, comment="#")


def _normalize_gsics_table(table: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "timestamp": ["timestamp", "valid_time", "time", "date"],
        "sensor": ["sensor", "monitored_sensor", "satellite_pair", "pair"],
        "reference_sensor": ["reference_sensor", "reference", "ref_sensor"],
        "band": ["band", "channel", "ch"],
        "correction_coefficient": ["correction_coefficient", "coefficient", "slope", "value"],
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
        normalized["source_agency"] = "GSICS"
    return normalized

