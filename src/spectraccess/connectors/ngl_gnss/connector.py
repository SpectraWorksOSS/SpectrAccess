"""NGL daily SINEX troposphere files from station/year ZIP archives.

Primary layout and column documentation:
https://geodesy.unr.edu/gps_timeseries/readmes/README_trop2.txt
https://geodesy.unr.edu/gps_timeseries/IGS20/trop/
https://geodesy.unr.edu/NGLStationPages/llh.out

The README's former /gps_timeseries/trop path is now under /IGS20/trop.
Its explicit gradient-column interchange warning applies to both values and
formal errors. Preserve reported fields and expose corrected directions.
Returns zenith total delay and the available tropospheric gradients.
"""

from __future__ import annotations

import calendar
import gzip
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from spectraccess.core.connector import Connector
from spectraccess.core.schema import frame_from_records

BASE_URL = "https://geodesy.unr.edu/gps_timeseries/IGS20/trop"
STATIONS_URL = "https://geodesy.unr.edu/NGLStationPages/llh.out"
README_URL = "https://geodesy.unr.edu/gps_timeseries/readmes/README_trop2.txt"


@dataclass(frozen=True)
class Station:
    site: str
    latitude: float
    longitude: float
    height_m: float
    source_url: str | None = None
    height_datum: str | None = None


@dataclass(frozen=True)
class NGLTarget:
    station: Station
    day: date
    source_url: str


@dataclass(frozen=True)
class NGLResult:
    content: bytes | str
    target: NGLTarget
    retrieved_at: datetime


def parse_stations(text: str, *, source_url: str | None = None) -> dict[str, Station]:
    """Read provider latitude, longitude and ellipsoidal height (metres)."""
    stations = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        site, lat, lon, height = line.split()
        longitude = (float(lon) + 180) % 360 - 180
        stations[site] = Station(site, float(lat), longitude, float(height), source_url, "ellipsoidal")
    return stations


class NGLGNSSConnector(Connector):
    """Public station/day discovery and bounded station/year access, no login."""

    def __init__(self, *, session: requests.Session | None = None, max_bytes: int = 10_000_000):
        self.session = session or requests.Session()
        self.max_bytes = max_bytes
        self._stations: dict[str, Station] | None = None

    def discover(self, *, station: str | Station, day: date, **_kwargs: object) -> list[NGLTarget]:
        site = station.site if isinstance(station, Station) else station.upper()
        if not re.fullmatch(r"[A-Z0-9]{4}", site):
            raise ValueError("station must be a four-character NGL code")
        if not isinstance(station, Station):
            if self._stations is None:
                self._stations = parse_stations(self._get(STATIONS_URL).decode("ascii"), source_url=STATIONS_URL)
            if site not in self._stations:
                raise ValueError(f"station {site} absent from NGL station metadata")
            station = self._stations[site]
        return [NGLTarget(station, day, f"{BASE_URL}/{site}/{site}.{day.year}.trop.zip")]

    def _get(self, url: str) -> bytes:
        with self.session.get(url, timeout=60, stream=True) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ValueError(f"NGL response exceeds max_bytes={self.max_bytes}")
                chunks.append(chunk)
            return b"".join(chunks)

    def fetch(self, target: NGLTarget, *, dest: str | Path | None = None, **_kwargs: object) -> NGLResult:
        payload = self._get(target.source_url)
        member = f"{target.station.site}.{target.day.year}.{target.day.timetuple().tm_yday:03d}.trop.gz"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            candidates = [n for n in archive.namelist() if Path(n).name == member]
            if len(candidates) != 1:
                raise ValueError(f"NGL archive has no unique daily member {member}")
            info = archive.getinfo(candidates[0])
            if info.file_size > self.max_bytes:
                raise ValueError("NGL daily member exceeds max_bytes")
            compressed = archive.read(candidates[0])
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as daily:
            raw = daily.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise ValueError("NGL decompressed daily member exceeds max_bytes")
        if dest is None:
            return NGLResult(raw, target, datetime.now(timezone.utc))
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return NGLResult(str(path), target, datetime.now(timezone.utc))

    def _parse_kwargs_for(self, target: NGLTarget) -> dict[str, object]:
        return {"target": target}

    _canonical_kwargs_for = _parse_kwargs_for

    def parse(self, raw: NGLResult | bytes | str, *, target: NGLTarget | None = None,
              station: Station | None = None, source_url: str | None = None,
              retrieved_at: datetime | None = None, correct_gradient_swap: bool = True) -> pd.DataFrame:
        if isinstance(raw, NGLResult):
            target = raw.target
            retrieved_at = retrieved_at or raw.retrieved_at
            raw = raw.content
        if isinstance(raw, str):
            raw = Path(raw).read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return parse_sinex(raw.decode("ascii"), station=target.station if target else station,
                           source_url=target.source_url if target else source_url,
                           retrieved_at=retrieved_at, correct_gradient_swap=correct_gradient_swap)

    parse_canonical = parse


def _epoch(text: str) -> datetime:
    yy, doy, seconds = map(int, text.split(":"))
    year = yy if yy >= 100 else (1900 + yy if yy >= 80 else 2000 + yy)
    if not 1 <= doy <= (366 if calendar.isleap(year) else 365) or not 0 <= seconds <= 86400:
        raise ValueError(f"invalid SINEX epoch {text}")
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, seconds=seconds)


def parse_sinex(text: str, *, station: Station | None = None, source_url: str | None = None,
                retrieved_at: datetime | None = None, correct_gradient_swap: bool = True) -> pd.DataFrame:
    """Read TROTOT and optional gradients by header, with formal errors in mm.

    The README describes _SIG as formal error columns. The five-minute
    sampling interval is retained in QA. SITE/ID supplies approximate location
    if precise provider llh metadata were not passed.
    """
    section = None
    fields: list[str] | None = None
    locations: dict[str, Station] = {}
    software = None
    sampling = None
    processing_inputs = None
    mapping_function = None
    reference_frame = re.search(r"\bIGS(?:14|20)\b", text.replace("IGS20_", "IGS20").replace("IGS14_", "IGS14"))
    rows = []
    closed = False
    for line in text.splitlines():
        if line.startswith("+"):
            section = line[1:].strip()
            continue
        if line.startswith("-"):
            if section == "TROP/SOLUTION":
                closed = True
            section = None
            continue
        if section == "FILE/REFERENCE" and line.strip().startswith("SOFTWARE"):
            software = line.strip()[len("SOFTWARE"):].strip()
        if section == "FILE/REFERENCE" and line.strip().startswith("INPUT"):
            processing_inputs = line.strip()[len("INPUT"):].strip()
        if section == "TROP/DESCRIPTION" and line.strip().startswith("SAMPLING TROP"):
            sampling = float(line.split()[-1])
        if section == "TROP/DESCRIPTION" and line.strip().startswith("TROP MAPPING FUNCTION"):
            mapping_function = line.strip()[len("TROP MAPPING FUNCTION"):].strip()
        if section == "SITE/ID" and line.strip() and not line.startswith("*"):
            tokens = line.split()
            if len(tokens) >= 11:
                lon_d, lon_m, lon_s, lat_d, lat_m, lat_s, height = map(float, tokens[-7:])
                lat = (-1 if lat_d < 0 else 1) * (abs(lat_d) + lat_m / 60 + lat_s / 3600)
                lon = (-1 if lon_d < 0 else 1) * (abs(lon_d) + lon_m / 60 + lon_s / 3600)
                locations[tokens[0]] = Station(tokens[0], lat, (lon + 180) % 360 - 180, height, None, "ellipsoidal")
        if section != "TROP/SOLUTION" or not line.strip():
            continue
        if line.startswith("*"):
            fields = line[1:].split()
            continue
        if fields is None or "TROTOT" not in fields:
            raise ValueError("NGL TROP/SOLUTION requires a TROTOT header")
        tokens = line.split()
        if len(tokens) != len(fields):
            raise ValueError("NGL solution row does not match its header")
        site = tokens[0]
        location = station or locations.get(site)
        if location is None or location.site != site:
            raise ValueError(f"station metadata missing or mismatched for {site}")
        timestamp = _epoch(tokens[1])
        reported = [{"field": key, "value": float(value)} for key, value in zip(fields[2:], tokens[2:])]
        for field, quantity in (("TROTOT", "zenith_total_delay"),
                                ("TGNTOT", "tropospheric_gradient_north"),
                                ("TGETOT", "tropospheric_gradient_east")):
            if field not in fields:
                continue
            read_field = field
            if correct_gradient_swap and field in {"TGNTOT", "TGETOT"}:
                quantity = "tropospheric_gradient_east" if field == "TGNTOT" else "tropospheric_gradient_north"
            index = fields.index(read_field)
            value = float(tokens[index])
            if not pd.notna(value) or value <= -999:
                continue
            sigma = None
            if index + 1 < len(fields) and fields[index + 1] in {"_SIG", "STDDEV"}:
                candidate = float(tokens[index + 1])
                if candidate >= 0:
                    sigma = candidate / 1000
            row = dict(time=timestamp, valid_time=timestamp, platform="ground",
                       instrument="GNSS", site=site, latitude=location.latitude,
                       longitude=location.longitude, elevation_m=location.height_m,
                       quantity=quantity, value=value / 1000, units="m",
                       unc_value=sigma, unc_status="provided" if sigma is not None else "unknown",
                       unc_k=None,
                       unc_provider="NGL formal error" if sigma is not None else None,
                       source="ngl-gnss", source_agency="Nevada Geodetic Laboratory",
                       source_url=source_url, retrieved_at=retrieved_at,
                       support_kind="point", footprint_geometry={"type": "Point", "coordinates": [location.longitude, location.latitude]},
                       algorithm_version=software, collection_version=reference_frame.group(0) if reference_frame else None,
                       unc_definition="Formal error columns (_SIG) for the tropospheric estimates",
                       qa={"gradient_columns_interchanged": correct_gradient_swap,
                           "gradient_warning_url": README_URL, "sampling_interval_s": sampling,
                           "location_basis": ("provider llh" if station.source_url == STATIONS_URL else "caller station metadata") if station else "approximate SINEX SITE/ID",
                           "elevation_datum": location.height_datum, "mapping_function": mapping_function},
                       reported_fields=reported, reported_field=read_field,
                       processing_inputs=processing_inputs, station_metadata_url=location.source_url)
            rows.append(row)
    if not closed or fields is None:
        raise ValueError("missing or truncated NGL TROP/SOLUTION block")
    return frame_from_records(rows)
