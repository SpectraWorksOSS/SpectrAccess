"""Regional surface pressure on CAMS EAC4's existing ADS route.

Primary variable availability and product description:
https://ads.atmosphere.copernicus.eu/api/catalogue/v1/collections/cams-global-reanalysis-eac4/constraints.json
https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4

EAC4 publishes surface_pressure (Pa) through ADS.
The product supplies no per-cell pressure uncertainty or observation averaging
kernel. Published coordinates and file metadata accompany each pressure row.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from spectraccess.core.schema import frame_from_records
from spectraccess.core.credentials import resolve, provider_error


@dataclass(frozen=True)
class CAMSSurfacePressureResult:
    path: Path
    source_url: str
    retrieved_at: datetime
    request: dict[str, object]
    dataset: str = "cams-global-reanalysis-eac4"


def fetch_surface_pressure(connector, *, valid_time: datetime,
                           area: tuple[float, float, float, float], dest: str | Path) -> CAMSSurfacePressureResult:
    from .connector import ADS_API_URL, ADS_DATASET, ADS_RETRIEVE_URL, ADS_TIMES, CAMSProviderError, _as_utc, _cds_client

    timestamp = _as_utc(valid_time)
    if timestamp.strftime("%H:%M") not in ADS_TIMES or timestamp.second or timestamp.microsecond:
        raise ValueError("surface pressure requires an exact EAC4 three-hour analysis time")
    if len(area) != 4 or not np.isfinite(area).all():
        raise ValueError("area requires four finite north, west, south, east coordinates")
    north, west, south, east = area
    if not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
        raise ValueError("invalid ADS area; use north, west, south, east")
    credential = resolve("ads", connector.credentials)
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    request = {"variable": ["surface_pressure"], "date": [timestamp.strftime("%Y-%m-%d")],
               "time": [timestamp.strftime("%H:%M")], "area": list(area), "data_format": "netcdf"}
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        partial = Path(tmp) / "pressure.nc"
        failure = None
        try:
            _cds_client(ADS_API_URL, credential.secret).retrieve(ADS_DATASET, request, str(partial))
            if not partial.exists() or not partial.stat().st_size:
                raise CAMSProviderError("ADS surface-pressure retrieval returned no data")
            # Validate science contents before replacing any existing destination.
            verified = parse_surface_pressure(str(partial))
            if verified.empty or not pd.to_datetime(verified.valid_time, utc=True).eq(timestamp).all():
                raise CAMSProviderError("ADS surface-pressure output has no valid data at the requested epoch")
            partial.replace(path)
        except Exception as exc:
            failure = provider_error("ads", connector.credentials, exc, credential, CAMSProviderError)
        if failure is not None:
            raise failure from None
    return CAMSSurfacePressureResult(path, ADS_RETRIEVE_URL, datetime.now(timezone.utc), request)


def parse_surface_pressure(raw: CAMSSurfacePressureResult | str | Path, *,
                           source_url: str | None = None, retrieved_at: datetime | None = None) -> pd.DataFrame:
    """Decode sp/surface_pressure NetCDF without supplying missing statistics.

    Coordinates describe cell centres. Footprint polygons and elevation are
    present only if the file publishes coordinate bounds or surface altitude.
    No integration duration is inferred from the three-hour output spacing.
    """
    if isinstance(raw, CAMSSurfacePressureResult):
        source_url, retrieved_at, path = raw.source_url, raw.retrieved_at, raw.path
    else:
        path = Path(raw)
    with xr.open_dataset(path) as ds:
        name = "sp" if "sp" in ds else "surface_pressure"
        if name not in ds:
            raise ValueError("CAMS NetCDF has no sp/surface_pressure")
        var = ds[name]
        if var.attrs.get("units") != "Pa":
            raise ValueError("surface_pressure must be published in Pa")
        time_name = "valid_time" if "valid_time" in var.coords else "time"
        if time_name not in var.coords or "latitude" not in var.coords or "longitude" not in var.coords:
            raise ValueError("surface pressure needs valid time, latitude and longitude coordinates")
        if not np.issubdtype(ds[time_name].dtype, np.datetime64):
            raise ValueError("surface-pressure time must decode to datetime")
        rows = []
        for record in var.to_dataframe(name="value").reset_index().to_dict("records"):
            v = record["value"]
            if not np.isfinite(v):
                continue
            if v <= 0:
                raise ValueError("surface pressure must be positive")
            lat, lon = float(record["latitude"]), float(record["longitude"])
            timestamp = pd.Timestamp(record[time_name])
            if pd.isna(timestamp) or not np.isfinite([lat, lon]).all():
                continue
            timestamp = timestamp.tz_localize("UTC")
            longitude = (lon + 180) % 360 - 180
            row = dict(time=timestamp, valid_time=timestamp, latitude=lat, longitude=longitude,
                       platform="model", instrument="IFS", support_kind="grid cell",
                       quantity="surface_air_pressure", value=float(v), units="Pa",
                       unc_value=None, unc_status="unknown", source="cams-eac4-surface-pressure",
                       source_agency="ECMWF / Copernicus Atmosphere Monitoring Service",
                       source_url=source_url, retrieved_at=retrieved_at,
                       collection_version="EAC4",
                       qa={"finite_positive_pressure": True, "cell_center_only": True})
            if ds.attrs.get("algorithm_version"):
                row["algorithm_version"] = str(ds.attrs["algorithm_version"])
            bounds = [ds[c].attrs.get("bounds") for c in ("longitude", "latitude")]
            if all(b and b in ds for b in bounds):
                xb = ds[bounds[0]].sel(longitude=lon).values.reshape(-1)
                yb = ds[bounds[1]].sel(latitude=lat).values.reshape(-1)
                if len(xb) == len(yb) == 2:
                    west, east = ((float(x) + 180) % 360 - 180 for x in xb)
                    south, north = map(float, yb)
                    row["footprint_geometry"] = {"type": "Polygon", "coordinates": [
                        [[west, south], [east, south], [east, north], [west, north], [west, south]]
                    ]}
                    row["qa"]["cell_center_only"] = False
            if "surface_altitude" in ds and ds.surface_altitude.attrs.get("units") == "m":
                altitude = ds.surface_altitude.sel(latitude=lat, longitude=lon)
                if time_name in altitude.dims:
                    altitude = altitude.sel({time_name: record[time_name]})
                if altitude.size == 1 and np.isfinite(altitude.item()):
                    row["elevation_m"] = float(altitude.item())
            rows.append(row)
        return frame_from_records(rows)
