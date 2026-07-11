"""NASA AERONET v3 web-service connector and CSV parser."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode

import pandas as pd
import requests

from spectraccess.core.connector import Connector
from spectraccess.core.schema import Uncertainty, UncertaintyStatus, empty_frame, uncertainty_columns, validate

DEFAULT_BASE_URL = "https://aeronet.gsfc.nasa.gov/cgi-bin/print_web_data_v3"

_NODATA = -900.0
_TARGET_NM = 550.0
_AOD_BAND_NM = sorted([340,380,400,412,440,443,490,500,510,531,532,551,555,560,620,667,675,681,709,779,865,870,1020,1640])
_AOD_BAND_COLS = {nm: f"AOD_{nm}nm" for nm in _AOD_BAND_NM}
_COL_DATE = "Date(dd:mm:yyyy)"
_COL_TIME = "Time(hh:mm:ss)"
_COL_ANGSTROM = "440-870_Angstrom_Exponent"
_COL_PW = "Precipitable_Water(cm)"
_COL_SITE = "AERONET_Site"
_COL_LAT = "Site_Latitude(Degrees)"
_COL_LON = "Site_Longitude(Degrees)"
_COL_ELEV = "Site_Elevation(m)"
_REQUIRED_COLS = (_COL_DATE, _COL_TIME, _COL_PW, _COL_SITE)
_LEVEL_FLAG = {"L2.0": "AOD20", "L1.5": "AOD15"}

NATIVE_COLUMNS = [
    # observation_index is the ordinal of the source CSV data row. Every band
    # row expanded from the same observation shares it, so a consumer can
    # regroup back to one-record-per-CSV-row identity (NOT timestamp -- two
    # observations could in principle share a timestamp; grouping on timestamp
    # would silently merge them).
    "observation_index",
    "timestamp", "site", "latitude", "longitude", "elevation_m", "data_level",
    "wavelength_nm", "aod", "pw_cm", "angstrom_440_870",
]


class AeronetSchemaError(ValueError):
    """AERONET CSV header is present but incompatible with this parser."""


class AeronetSiteMismatchError(ValueError):
    """A response contains data for a site other than the requested site."""


def _empty_native_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "observation_index": pd.Series([], dtype="int64"),
        "timestamp": pd.Series([], dtype="datetime64[ns, UTC]"),
        "site": pd.Series([], dtype="object"),
        "latitude": pd.Series([], dtype="float64"),
        "longitude": pd.Series([], dtype="float64"),
        "elevation_m": pd.Series([], dtype="float64"),
        "data_level": pd.Series([], dtype="object"),
        "wavelength_nm": pd.Series([], dtype="float64"),
        "aod": pd.Series([], dtype="float64"),
        "pw_cm": pd.Series([], dtype="float64"),
        "angstrom_440_870": pd.Series([], dtype="float64"),
    })


def pick_bracket_bands(nm_values: Mapping[int, float]) -> tuple[int, int] | None:
    """Choose the best two available wavelengths for interpolation at 550 nm."""
    bands = sorted(nm for nm, value in nm_values.items() if value > 0)
    if len(bands) < 2:
        return None
    below = [nm for nm in bands if nm <= _TARGET_NM]
    above = [nm for nm in bands if nm >= _TARGET_NM]
    if below and above:
        lo, hi = max(below), min(above)
        if lo != hi:
            return lo, hi
        other = min((nm for nm in bands if nm != lo), key=lambda nm: (abs(nm - lo), nm))
        return tuple(sorted((lo, other)))
    nearest = sorted(bands, key=lambda nm: (abs(nm - _TARGET_NM), nm))[:2]
    return tuple(sorted(nearest))


def interpolate_aod_bracket(
    nm_lo: int, aod_lo: float, nm_hi: int, aod_hi: float, target_nm: float = _TARGET_NM
) -> float:
    """Interpolate AOD using the Angstrom log-log power law."""
    if aod_lo <= 0 or aod_hi <= 0:
        raise ValueError("AOD values must be positive")
    if nm_lo == nm_hi:
        raise ValueError("interpolation wavelengths must be distinct")
    slope = (math.log(aod_hi) - math.log(aod_lo)) / (math.log(nm_hi) - math.log(nm_lo))
    return math.exp(math.log(aod_lo) + slope * (math.log(target_nm) - math.log(nm_lo)))


def interpolate_aod_550(nm_values: Mapping[int, float]) -> tuple[float, int, int] | None:
    bracket = pick_bracket_bands(nm_values)
    if bracket is None:
        return None
    lo, hi = bracket
    return interpolate_aod_bracket(lo, nm_values[lo], hi, nm_values[hi]), lo, hi


def _number(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if result > _NODATA else float("nan")


def parse_aeronet_csv(
    text: str, *, requested_site: str | None = None, data_level: str = "L2.0"
) -> pd.DataFrame:
    """Parse an AERONET v3 CSV into one row per observation and present AOD band."""
    lines = text.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if _COL_DATE in line and _COL_TIME in line), None
    )
    if header_index is None:
        return _empty_native_frame()

    reader = csv.DictReader(StringIO("\n".join(lines[header_index:])))
    fields = reader.fieldnames or []
    missing = [column for column in _REQUIRED_COLS if column not in fields]
    if missing:
        raise AeronetSchemaError(f"AERONET header missing required columns: {missing}")
    present_bands = [(nm, col) for nm, col in _AOD_BAND_COLS.items() if col in fields]
    if len(present_bands) < 2:
        raise AeronetSchemaError("AERONET header contains fewer than 2 recognised AOD bands")

    rows: list[dict[str, object]] = []
    # enumerate gives each source CSV data row a stable ordinal so all its band
    # rows regroup to one observation identity downstream (see NATIVE_COLUMNS).
    for observation_index, source_row in enumerate(reader):
        site = source_row.get(_COL_SITE)
        if requested_site is not None and site != requested_site:
            raise AeronetSiteMismatchError(
                f"AERONET response site {site!r} does not match requested site {requested_site!r}"
            )
        try:
            timestamp = pd.to_datetime(
                f"{source_row[_COL_DATE]} {source_row[_COL_TIME]}",
                format="%d:%m:%Y %H:%M:%S", utc=True,
            )
        except (KeyError, TypeError, ValueError):
            continue
        latitude = _number(source_row.get(_COL_LAT))
        longitude = _number(source_row.get(_COL_LON))
        elevation = _number(source_row.get(_COL_ELEV))
        pw_cm = _number(source_row.get(_COL_PW))
        angstrom = _number(source_row.get(_COL_ANGSTROM))
        for nm, column in present_bands:
            aod = _number(source_row.get(column))
            if pd.isna(aod) or aod <= 0:
                continue
            rows.append({
                "observation_index": observation_index,
                "timestamp": timestamp, "site": site, "latitude": latitude,
                "longitude": longitude, "elevation_m": elevation, "data_level": data_level,
                "wavelength_nm": float(nm), "aod": aod, "pw_cm": pw_cm,
                "angstrom_440_870": angstrom,
            })
    if not rows:
        return _empty_native_frame()
    return pd.DataFrame(rows, columns=NATIVE_COLUMNS).sort_values(
        ["timestamp", "wavelength_nm"], ignore_index=True
    )


@dataclass(frozen=True)
class AeronetTarget:
    site: str
    start: datetime
    end: datetime
    level: str
    url: str


class AeronetConnector(Connector):
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 30) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._session = requests.Session()

    def discover(
        self, site: str, start: datetime, end: datetime, *, level: str = "L2.0", **_kw: object
    ) -> list[AeronetTarget]:
        if level not in _LEVEL_FLAG:
            raise ValueError(f"level must be one of {sorted(_LEVEL_FLAG)}")
        start_utc = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end_utc = end.astimezone(timezone.utc) if end.tzinfo else end.replace(tzinfo=timezone.utc)
        if end_utc < start_utc:
            raise ValueError(f"end must be >= start (got start={start_utc}, end={end_utc})")
        params = {
            "site": site, "year": start_utc.year, "month": start_utc.month, "day": start_utc.day,
            "year2": end_utc.year, "month2": end_utc.month, "day2": end_utc.day,
            _LEVEL_FLAG[level]: 1, "AVG": 10, "if_no_html": 1,
        }
        url = f"{self.base_url}?{urlencode(params)}"
        return [AeronetTarget(site, start, end, level, url)]

    def fetch(self, target: AeronetTarget, *, dest=None, **_kw: object) -> bytes | str:
        response = self._session.get(
            target.url, headers={"User-Agent": "spectrAccess AERONET connector"}, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.text.encode(response.encoding or "utf-8")
        if dest is None:
            return payload
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return str(path)

    def parse(
        self, raw, *, requested_site: str | None = None, data_level: str = "L2.0"
    ) -> pd.DataFrame:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        elif isinstance(raw, Path):
            text = Path(raw).read_text(encoding="utf-8", errors="replace")
        elif isinstance(raw, str) and "\n" not in raw and Path(raw).exists():
            text = Path(raw).read_text(encoding="utf-8", errors="replace")
        else:
            text = str(raw)
        return parse_aeronet_csv(text, requested_site=requested_site, data_level=data_level)

    def parse_canonical(
        self, raw, *, source_url=None, retrieved_at=None, requested_site=None, data_level="L2.0"
    ) -> pd.DataFrame:
        native = self.parse(raw, requested_site=requested_site, data_level=data_level)
        return to_canonical(native, source_url=source_url, retrieved_at=retrieved_at)

    def _parse_kwargs_for(self, target: object) -> dict[str, object]:
        if isinstance(target, AeronetTarget):
            return {"requested_site": target.site, "data_level": target.level}
        return {}

    def _canonical_kwargs_for(self, target: object) -> dict[str, object]:
        if isinstance(target, AeronetTarget):
            return {"source_url": target.url, "requested_site": target.site, "data_level": target.level}
        return {}


def to_canonical(native: pd.DataFrame, *, source_url=None, retrieved_at=None) -> pd.DataFrame:
    """Emit canonical AOD rows plus one precipitable-water row per observation."""
    if native.empty:
        return empty_frame()
    unc = Uncertainty(value=None, status=UncertaintyStatus.UNKNOWN)
    # AERONET's published ~0.01-0.02 L2.0 budget may become a future prior;
    # v1 deliberately does not assert it as per-observation uncertainty.
    rows: list[dict[str, object]] = []

    def base(row: pd.Series) -> dict[str, object]:
        result = {
            "time": row.get("timestamp"), "platform": None, "instrument": None, "band": None,
            "site": row.get("site"), "latitude": row.get("latitude"),
            "longitude": row.get("longitude"), "reference": None, "source": "aeronet",
            "source_agency": "NASA AERONET", "source_url": source_url, "retrieved_at": retrieved_at,
            "pw_cm": row.get("pw_cm"), "angstrom_440_870": row.get("angstrom_440_870"),
            "data_level": row.get("data_level"), "elevation_m": row.get("elevation_m"),
        }
        result.update(uncertainty_columns(unc))
        return result

    for _, row in native.iterrows():
        output = base(row)
        output.update({"wavelength_nm": float(row["wavelength_nm"]), "quantity": "aerosol_optical_depth", "value": float(row["aod"]), "units": "1"})
        rows.append(output)

    # One PW row per observation (keyed on observation_index, not timestamp:
    # two sites -- or two observations -- can share a timestamp, and keying on
    # timestamp alone would silently drop one observation's PW).
    pw_rows = native.loc[native["pw_cm"].notna()].drop_duplicates(subset=["observation_index"])
    for _, row in pw_rows.iterrows():
        output = base(row)
        output.update({"wavelength_nm": float("nan"), "quantity": "precipitable_water", "value": float(row["pw_cm"]), "units": "cm"})
        rows.append(output)
    return validate(pd.DataFrame(rows))
