"""OL_2_LFR IWV through CDSETool, using the Sentinel-2 auth/transport pattern.

Product variables, uncertainty, time coordinates and LQSF flags:
https://sentiwiki.copernicus.eu/web/olci-products
CDSE query and authenticated product-node access:
https://documentation.dataspace.copernicus.eu/APIs/OData.html
Published positive wet bias (7 to 10 percent):
https://doi.org/10.5194/amt-15-5129-2022

The validation range is retained in published_bias with its reference and scope;
observations are not corrected using that range.
"""

from __future__ import annotations

import itertools
import io
import tempfile
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree

import numpy as np
import pandas as pd
import xarray as xr

try:
    from cdsetool.download import download_feature
    from cdsetool.query import query_features
except ImportError as exc:  # pragma: no cover - optional installation boundary
    raise ImportError("OLCICDSEConnector requires pip install 'spectraccess[cdse]'") from exc

from spectraccess.connectors.sentinel2_cdse.connector import _CaptureLogger, _ExplicitCredentials, _as_utc, _bbox_to_wkt
from spectraccess.core.credentials import Credential, CredentialSource, resolve, provider_error
from spectraccess.core.connector import Connector
from spectraccess.core.schema import frame_from_records

PRODUCT_TYPE = "OL_2_LFR___"
COLLECTION = "SENTINEL-3"
PRODUCT_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})"
WET_BIAS = {
    "relative_range": [0.07, 0.10], "sign": "positive", "applied": False,
    "basis": "cloud-free land validation of OLCI-A/B, not a per-pixel estimate",
    "reference": "https://doi.org/10.5194/amt-15-5129-2022",
}
_FILES = ("iwv.nc", "geo_coordinates.nc", "time_coordinates.nc", "lqsf.nc", "xfdumanifest.xml")
_REJECT_FLAGS = {"INVALID", "CLOUD", "CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "SNOW_ICE", "SATURATED", "SUSPECT", "WVFAIL"}


@dataclass(frozen=True)
class OLCITarget:
    product_id: str
    title: str
    source_url: str
    retrieved_at: datetime
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class OLCIResult:
    path: Path
    target: OLCITarget
    retrieved_at: datetime


class OLCICDSEConnector(Connector):
    """Discover LFR products and fetch just IWV and its annotation files.

    Downloads use the OS keyring or a credential source supplied in code.
    Catalogue search
    is public. Installing spectraccess[cdse] provides CDSETool and the
    HDF5 NetCDF backend needed for real OLCI files.
    """

    credential_provider = "cdse"

    def __init__(self, *, credentials: CredentialSource | None = None) -> None:
        self.credentials = credentials

    def discover(self, *, bbox: tuple[float, float, float, float], start: date | datetime,
                 end: date | datetime, limit: int = 10, **_kwargs: object) -> list[OLCITarget]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if limit == 0:
            return []
        start_utc, end_utc = _as_utc(start), _as_utc(end)
        if end_utc < start_utc:
            raise ValueError("end must be >= start")
        terms = {"productType": PRODUCT_TYPE, "geometry": _bbox_to_wkt(bbox),
                 "contentDateStartGe": start_utc.date().isoformat(),
                 "contentDateStartLt": (end_utc.date() + timedelta(days=1)).isoformat(),
                 "top": min(limit, 1000)}
        log = _CaptureLogger()
        try:
            features = list(itertools.islice(query_features(
                COLLECTION, terms, options={"logger": log, "expand_attributes": True},
            ), limit))
        except Exception:
            raise RuntimeError("OLCI CDSE catalogue query failed") from None
        if log.errors:
            raise RuntimeError("OLCI CDSE catalogue reported a provider error")
        targets = []
        for feature in features:
            attrs = {a["Name"]: a["Value"] for a in feature.get("Attributes", [])}
            if attrs.get("productType") != PRODUCT_TYPE or "OL_2_LFR" not in feature.get("Name", ""):
                raise ValueError("CDSE returned a product outside OL_2_LFR")
            raw = deepcopy(feature)
            raw["Collection"] = COLLECTION
            product_id = raw["Id"]
            targets.append(OLCITarget(product_id, raw["Name"], PRODUCT_URL.format(product_id=product_id),
                                      datetime.now(timezone.utc), raw))
        return targets

    def fetch(self, target: OLCITarget, *, dest: str | Path, username: str | None = None,
              password: str | None = None, credentials: CredentialSource | None = None,
              **_kwargs: object) -> OLCIResult:
        if credentials is not None and (username is not None or password is not None):
            raise ValueError("pass credentials or username/password, not both")
        if (username is None) != (password is None):
            raise ValueError("CDSE username and password must be supplied together")
        source = credentials if credentials is not None else self.credentials
        if username is not None and password is not None:
            source = Credential("password", password, username)
        credential = resolve("cdse", source)
        path = Path(dest)
        path.mkdir(parents=True, exist_ok=True)
        log = _CaptureLogger()
        options = {"logger": log, "filter_pattern": r"(?:iwv|geo_coordinates|time_coordinates|lqsf)\.nc$"}
        failure = None
        try:
            options["credentials"] = _ExplicitCredentials(credential.account, credential.secret)
            name = download_feature(deepcopy(dict(target.raw)), str(path), options)
        except Exception as exc:
            failure = provider_error("cdse", source, exc, credential, RuntimeError)
        if failure is not None:
            raise failure from None
        if log.errors or not name or not (path / name).is_dir():
            raise RuntimeError("OLCI CDSE download produced no complete product directory")
        product = path / name
        if any(not (product / n).is_file() for n in _FILES[:4]):
            raise RuntimeError("OLCI download is missing required IWV annotation files")
        return OLCIResult(product, target, datetime.now(timezone.utc))

    def _parse_kwargs_for(self, target: OLCITarget) -> dict[str, object]:
        return {"target": target}

    _canonical_kwargs_for = _parse_kwargs_for

    def parse(self, raw: OLCIResult | bytes | str, *, target: OLCITarget | None = None,
              source_url: str | None = None, retrieved_at: datetime | None = None,
              bbox: tuple[float, float, float, float] | None = None,
              include_flagged: bool = False) -> pd.DataFrame:
        """Canonical pixel IWV, preserving QA and scale/fill decoding.

        Directory or ZIP must contain IWV, geolocation, line times and LQSF.
        No product-wide time is substituted for a missing line observation time.
        Pixel centres are retained separately; no pixel polygon is invented.
        """
        if isinstance(raw, OLCIResult):
            target = raw.target
            retrieved_at = retrieved_at or raw.retrieved_at
            raw = str(raw.path)
        path = Path(raw) if isinstance(raw, str) else None
        if path is not None and path.is_dir():
            return _parse_product(path, target=target, source_url=source_url, retrieved_at=retrieved_at,
                                  bbox=bbox, include_flagged=include_flagged)
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(path if path is not None else io.BytesIO(raw)) as archive:
                for name in _FILES:
                    matches = [n for n in archive.namelist() if Path(n).name == name]
                    if len(matches) > 1:
                        raise ValueError(f"multiple OLCI {name} assets in archive")
                    if matches:
                        # Known basenames only; never extract arbitrary ZIP paths.
                        (Path(tmp) / name).write_bytes(archive.read(matches[0]))
            return _parse_product(Path(tmp), target=target, source_url=source_url, retrieved_at=retrieved_at,
                                  bbox=bbox, include_flagged=include_flagged)

    parse_canonical = parse


def _manifest_version(path: Path) -> str | None:
    if not path.exists():
        return None
    for node in ElementTree.parse(path).iter():
        if node.tag.split("}")[-1] == "software" and node.get("version"):
            return node.get("version")
    return None


def _parse_product(path: Path, *, target: OLCITarget | None, source_url: str | None,
                   retrieved_at: datetime | None, bbox: tuple[float, float, float, float] | None,
                   include_flagged: bool) -> pd.DataFrame:
    datasets = []
    try:
        for name in _FILES[:4]:
            datasets.append(xr.open_dataset(path / name))
        iwv, geo, times, flags = datasets
        value = iwv["IWV"]
        if value.ndim != 2:
            raise ValueError("OLCI IWV must have rows and columns")
        units = value.attrs.get("units", "")
        if units not in {"kg m-2", "kg.m-2", "kg m^-2", "kg/m^2"}:
            raise ValueError(f"unexpected IWV units {units!r}")
        if geo["latitude"].shape != value.shape or geo["longitude"].shape != value.shape:
            raise ValueError("OLCI geolocation shape does not match IWV")
        qa = flags["LQSF"]
        if qa.shape != value.shape:
            raise ValueError("OLCI QA shape does not match IWV")
        meanings = qa.attrs.get("flag_meanings", "").split()
        masks = qa.attrs.get("flag_masks", [])
        if not meanings or len(meanings) != len(masks) or not _REJECT_FLAGS.issubset(meanings):
            raise ValueError("OLCI QA lacks the documented flag definitions")
        flag_defs = dict(zip(meanings, (int(m) for m in masks)))
        stamp = times["time_stamp"]
        if stamp.size != value.shape[0] or not np.issubdtype(stamp.dtype, np.datetime64):
            raise ValueError("OLCI line time_stamp must decode to one datetime per row")
        error = iwv.get("IWV_err")
        if error is not None and error.shape != value.shape:
            raise ValueError("OLCI IWV_err shape does not match IWV")
        algorithm = _manifest_version(path / "xfdumanifest.xml")
        attrs = {} if target is None else {a["Name"]: a["Value"] for a in target.raw.get("Attributes", [])}
        algorithm = attrs.get("processorVersion") or algorithm
        collection = attrs.get("baselineCollection")
        rows = []
        values, latitudes, longitudes, qa_values = value.values, geo.latitude.values, geo.longitude.values, qa.values
        usable = np.isfinite(values) & np.isfinite(latitudes) & np.isfinite(longitudes) & pd.notna(qa_values)
        if bbox is not None:
            usable &= (longitudes >= bbox[0]) & (longitudes <= bbox[2]) & (latitudes >= bbox[1]) & (latitudes <= bbox[3])
        errors = error.values if error is not None else None
        line_times = stamp.values.reshape(-1)
        for i, j in zip(*np.nonzero(usable)):
            v = float(values[i, j])
            lat, lon = float(latitudes[i, j]), float(longitudes[i, j])
            flag_value = int(qa_values[i, j])
            active = [name for name, mask in flag_defs.items() if flag_value & mask]
            accepted = not _REJECT_FLAGS.intersection(active)
            if not accepted and not include_flagged:
                continue
            timestamp = pd.Timestamp(line_times[i])
            if pd.isna(timestamp):
                continue
            timestamp = timestamp.tz_localize("UTC")
            sigma = float(errors[i, j]) if errors is not None else None
            if sigma is not None and (not np.isfinite(sigma) or sigma < 0):
                sigma = None
            row = dict(time=timestamp, valid_time=timestamp,
                       platform=target.title[:3] if target else None, instrument="OLCI",
                       quantity="atmosphere_mass_content_of_water_vapor", value=v, units="kg m-2",
                       latitude=lat, longitude=lon, support_kind="pixel",
                       unc_value=sigma, unc_status="provided" if sigma is not None else "unknown",
                       unc_k=1 if sigma is not None else None,
                       unc_provider="OLCI IWV_err" if sigma is not None else None,
                       source="sentinel-3-olci-l2-iwv", source_agency="European Union / ESA",
                       source_url=target.source_url if target else source_url,
                       retrieved_at=retrieved_at, algorithm_version=algorithm,
                       collection_version=collection,
                       unc_definition="Uncertainty estimate for the Integrated water vapour column above the current pixel",
                       published_bias=deepcopy(WET_BIAS), product_type=PRODUCT_TYPE,
                       pixel_row=i, pixel_column=j,
                       qa={"value": flag_value, "flags": active, "accepted": accepted,
                           "flag_masks": flag_defs},
                       pixel_center_geometry={"type": "Point", "coordinates": [lon, lat]})
            if target is not None:
                row.update(product_id=target.product_id, product_title=target.title,
                           product_footprint=deepcopy(target.raw.get("GeoFootprint")),
                           provider_metadata=deepcopy(dict(target.raw)))
            if "altitude" in geo:
                height = float(geo.altitude.values[i, j])
                if np.isfinite(height):
                    row["elevation_m"] = height
                    row["qa"]["elevation_metadata"] = dict(geo.altitude.attrs)
            # Prior state and averaging kernel are not published in OL_2_LFR.
            rows.append(row)
        return frame_from_records(rows)
    finally:
        for dataset in datasets:
            dataset.close()
