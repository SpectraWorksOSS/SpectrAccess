"""MSG SEVIRI Level 1.5 provider observations in native geometry."""
from __future__ import annotations

import hashlib
import itertools
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import numpy as np
import xarray as xr

from spectraccess.connectors._eumetsat import (
    EUMETSATAuthorizationError, EUMETSATProviderError, _auth_values,
    _eumdac_context, _provider_failure as _shared_provider_failure,
)
from spectraccess.core.connector import Connector
from spectraccess.core.credentials import CredentialSource, resolve
from spectraccess.core.schema import frame_from_records

try:
    import eumdac
    import satpy
    from pyproj import Transformer
    from satpy.dataset import DataID
    from satpy.readers.seviri_l1b_native import NativeMSGFileHandler
    from satpy.readers.core.seviri import SEVIRICalibrationAlgorithm, get_cds_time
except ImportError as exc:
    raise ImportError("SEVIRIConnector requires pip install 'spectraccess[seviri]' (Python >=3.11)") from exc

COLLECTIONS = {"full_disc": "EO:EUM:DAT:MSG:HRSEVIRI", "rapid_scan": "EO:EUM:DAT:MSG:MSG15-RSS"}
CHANNELS = ("VIS006", "VIS008", "IR_016", "IR_039", "WV_062", "WV_073",
            "IR_087", "IR_097", "IR_108", "IR_120", "IR_134", "HRV")
DEFAULT_CHANNELS = ("IR_108", "VIS006")
COVERAGE_SOURCE = "https://raw.githubusercontent.com/pytroll/satpy/v0.60.0/satpy/readers/seviri_l1b_native.py#NativeMSGFileHandler.is_roi"
ROW_CONVENTION = "ICD 105 one-based full-disc fixed grid from south-east; array row 0 is southernmost available row; south to north; HRV uses its own grid"


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bbox(value):
    if value is None:
        return None
    west, south, east, north = map(float, value)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be (west, south, east, north), without crossing the antimeridian")
    return west, south, east, north


def _channels(value):
    value = tuple(value)
    if not value or set(value) - set(CHANNELS):
        raise ValueError(f"channels must select from {CHANNELS}")
    return value


@dataclass(frozen=True)
class SEVIRITarget:
    product_id: str
    collection: str
    service: str
    platform: str | None
    sensing_start: datetime
    sensing_end: datetime
    size: Any
    raw: Mapping[str, Any]
    source_url: str
    retrieved_at: datetime
    bbox: tuple[float, float, float, float] | None = None

    @property
    def footprint(self):
        return None

    @property
    def service_coverage(self):
        return {"basis": "connector-declared nominal service coverage, not a product footprint",
                "grid": "VIS/IR", "south_line": 1 if self.service == "full_disc" else 2321,
                "north_line": 3712, "grid_lines": 3712, "source": COVERAGE_SOURCE,
                "provider_numerical_rss_table_verified": False}

    @property
    def time_window(self):
        return {"start": self.sensing_start, "end": self.sensing_end}


@dataclass(frozen=True)
class SEVIRIResult:
    path: Path
    target: SEVIRITarget
    retrieved_at: datetime
    source_entry: str | None = None


class SEVIRIProviderError(EUMETSATProviderError):
    """Redacted provider failure with HTTP status and stage."""


class SEVIRIAuthorizationError(EUMETSATAuthorizationError, SEVIRIProviderError):
    """An EUMETSAT account lacks access to the named collection."""
    provider_name = "SEVIRI"


def _provider_failure(exc, stage, collection=None, *, values=()):
    return _shared_provider_failure(exc, stage, collection, values=values, provider_name="SEVIRI",
                                    provider_error=SEVIRIProviderError, authorization_error=SEVIRIAuthorizationError)


class _UnclippedCalibration(SEVIRICalibrationAlgorithm):
    def convert_to_radiance(self, data, gain, offset):
        # Satpy's unconditional zero clipping has no pinned provider basis.
        # Keep its coefficient selection and invalid-count mask; disable clipping.
        return data.where(data > 0) * gain + offset


class _NativeHandler(NativeMSGFileHandler):
    def _add_scanline_acq_time(self, dataset, dataset_id):
        # Pinned reader leaves a singleton channel axis for one-channel files
        # and get_cds_time returns a scalar for one-row files. Keep row support.
        raw = (self._get_acq_time_hrv() if dataset_id["name"] == "HRV"
               else self._get_acq_time_visir(dataset_id)).reshape(-1)
        times = get_cds_time(raw["Days"], raw["Milliseconds"])
        dataset.coords["acq_time"] = ("y", np.atleast_1d(times))

    def _get_calibration_handler(self, dataset_id):
        calibration = super()._get_calibration_handler(dataset_id)
        calibration._algo = _UnclippedCalibration(self.platform_id, self.observation_start_time)
        return calibration


def _handler(result):
    return _NativeHandler(str(result.path), {}, {"file_type": "seviri_l1b_native"},
                          calib_mode="nominal", fill_disk=False, ext_calib_coefs={})


def _key(channel, calibration="counts"):
    from satpy.dataset.dataid import default_id_keys_config
    return DataID(default_id_keys_config, name=channel, resolution=1000 if channel == "HRV" else 3000, calibration=calibration)


def _line_info(handler, channel):
    if channel not in handler.mda["channel_list"]:
        raise ValueError(f"native product lacks requested channel {channel}")
    if channel == "HRV":
        records = handler._dask_array["hrv"]
    else:
        index = handler.mda["channel_list"].index(channel)
        records = handler._dask_array["visir"][:, index]
    # Field reads preserve channel-specific line side info without decoding pixels.
    info = {name: np.asarray(records[name].compute()).reshape(-1)
            for name in ("lineno", "chan_id", "acq_time", "line_validity", "line_rquality", "line_gquality")}
    # Native records are big-endian; the pinned reader declares this field it never
    # uses with native byte order, so decode the provider bytes explicitly.
    lineno = info["lineno"]
    if lineno.dtype.byteorder != ">":
        lineno = lineno.view(lineno.dtype.newbyteorder(">"))
    info["lineno"] = lineno.astype(np.int64)
    return info


def _facts(handler):
    header = handler.header["15_DATA_HEADER"]
    stats = handler.trailer["15TRAILER"]["ImageProductionStats"]
    orbit = handler._get_orbital_parameters()
    actual = all(name in orbit for name in ("satellite_actual_longitude", "satellite_actual_latitude", "satellite_actual_altitude"))
    return {"actual_coverage": {"VIS_IR": deepcopy(stats["ActualL15CoverageVIS_IR"]),
                                "HRV": deepcopy(stats["ActualL15CoverageHRV"])},
            "actual_coverage_source": "native trailer 15TRAILER/ImageProductionStats/ActualL15CoverageVIS_IR and ActualL15CoverageHRV",
            "selected_rectangle": deepcopy(handler.header["15_SECONDARY_PRODUCT_HEADER"]),
            "provider_product_qa": deepcopy(stats["L15ImageValidity"]),
            "provider_scanning_summary": deepcopy(stats["ActualScanningSummary"]),
            "orbital_parameters": orbit, "satellite_actual_position_available": actual,
            "satellite_position_source": "native header SatelliteStatus/Orbit/OrbitPolynomial evaluated by Satpy at observation_start_time; no catalogue position",
            "provider_orbit": deepcopy(header["SatelliteStatus"]["Orbit"]),
            "provider_satellite_definition": deepcopy(header["SatelliteStatus"]["SatelliteDefinition"]),
            "provider_image_description": deepcopy(header["ImageDescription"]),
            "provider_earth_model": deepcopy(header["GeometricProcessing"]["EarthModel"])}


def _areas(area):
    return list(area.defs) if hasattr(area, "defs") else [area]


def _window(area, bbox):
    if bbox is None:
        return slice(0, area.height), slice(0, area.width)
    rr_min, rr_max, cc_min, cc_max = area.height, -1, area.width, -1
    offset = 0
    for part in _areas(area):
        x, y = part.get_proj_vectors()
        inverse = Transformer.from_crs(part.crs, "EPSG:4326", always_xy=True)
        for first in range(0, part.height, 128):
            xx, yy = np.meshgrid(x, y[first:first + 128])
            lon, lat = inverse.transform(xx, yy)
            rows, cols = np.where((lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3]))
            if rows.size:
                rr_min, rr_max = min(rr_min, offset + first + rows.min()), max(rr_max, offset + first + rows.max())
                cc_min, cc_max = min(cc_min, cols.min()), max(cc_max, cols.max())
        offset += part.height
    if rr_max < 0:
        return slice(0, 0), slice(0, 0)
    return slice(int(rr_min), int(rr_max) + 1), slice(int(cc_min), int(cc_max) + 1)


def _projected_coords(area, rows, cols):
    xs, ys, offset = [], [], 0
    for part in _areas(area):
        x, y = part.get_proj_vectors()
        start, stop = max(rows.start - offset, 0), min(rows.stop - offset, part.height)
        if start < stop:
            xx, yy = np.meshgrid(x[cols], y[start:stop])
            xs.append(xx)
            ys.append(yy)
        offset += part.height
    shape = (rows.stop - rows.start, cols.stop - cols.start)
    return (np.concatenate(xs), np.concatenate(ys)) if xs else (np.empty(shape), np.empty(shape))


class SEVIRIConnector(Connector):
    """Data Store discovery, whole native-file fetch and native provider observations."""
    credential_provider = "eumetsat"

    def __init__(self, *, credentials: CredentialSource | None = None):
        self.credentials = credentials

    def _store(self, credentials=None):
        credential = resolve("eumetsat", credentials if credentials is not None else self.credentials)
        token = None
        try:
            token = eumdac.AccessToken((credential.account, credential.secret))
            with _eumdac_context(credential, token):
                str(token)
            return eumdac.DataStore(token)
        except Exception as exc:
            raise _provider_failure(exc, "token authentication", values=_auth_values(credential, token)) from exc

    def discover(self, *, bbox=None, start, end, services=("full_disc", "rapid_scan"), limit=None):
        """Return products; validate/record bbox but never filter absent catalogue geometry.

        limit bounds targets per service. None means all matching products.
        Catalogue date is the measured sensing interval, not the nominal cycle.
        EUMDAC search uses the same eumetsat credential resolution as fetch.
        """
        bbox, start, end = _bbox(bbox), _utc(start), _utc(end)
        services = tuple(services)
        if not services or set(services) - set(COLLECTIONS):
            raise ValueError(f"services must select from {tuple(COLLECTIONS)}")
        if end < start or (limit is not None and (not isinstance(limit, int) or limit < 0)):
            raise ValueError("end must be >= start and limit must be a nonnegative integer or None")
        if limit == 0:
            return []
        store, targets = self._store(), []
        with _eumdac_context(token=store.token):
            for service in services:
                collection = COLLECTIONS[service]
                try:
                    found = store.get_collection(collection).search(dtstart=start, dtend=end)
                    for product in itertools.islice(found, limit):
                        raw = deepcopy(product.metadata)
                        props = raw["properties"]
                        begin, finish = props["date"].split("/")
                        acquisitions = props.get("acquisitionInformation", [])
                        platform = acquisitions[0].get("platform", {}).get("platformShortName") if acquisitions else None
                        url = (f"https://api.eumetsat.int/data/download/1.0.0/collections/{quote(collection, safe='')}"
                               f"/products/{quote(str(product), safe='')}/metadata?format=json")
                        targets.append(SEVIRITarget(str(product), collection, service, platform, _utc(begin), _utc(finish),
                                                    props.get("productInformation", {}).get("size"), raw, url,
                                                    datetime.now(timezone.utc), bbox))
                except Exception as exc:
                    raise _provider_failure(exc, "catalogue query", collection, values=_auth_values(token=store.token)) from exc
        return targets

    def fetch(self, target, *, dest=None, credentials=None):
        """Download one complete product ZIP and extract its native file, without subsetting."""
        store = self._store(credentials)
        folder = Path(dest) if dest is not None else Path("data/seviri")
        folder = folder / hashlib.sha256((target.collection + target.product_id).encode()).hexdigest()[:20]
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "product.zip.part"
        partial = folder / "product.nat.part"
        output = folder / "product.nat"
        with _eumdac_context(token=store.token):
            try:
                product = store.get_product(target.collection, target.product_id)
                with product.open() as source, archive.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                with zipfile.ZipFile(archive) as zipped:
                    entries = [name for name in zipped.namelist() if name.lower().endswith(".nat") and not name.endswith("/")]
                    if len(entries) != 1:
                        raise ValueError("expected exactly one native .nat file in product ZIP")
                    # Stream to a fixed filename; never extract provider paths.
                    with zipped.open(entries[0]) as source, partial.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                partial.replace(output)
            except Exception as exc:
                raise _provider_failure(exc, "product fetch", target.collection, values=_auth_values(token=store.token)) from exc
            finally:
                archive.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
        return SEVIRIResult(output, target, datetime.now(timezone.utc), entries[0])

    def line_times(self, result, *, channel="IR_108"):
        """Return provider line mean times, ICD row numbers and unmodified line QA.

        Rows retain native south-to-north order; HRV numbers address its own grid.
        Actual trailer coverage and header selected rectangle are separate attrs.
        No pixel calibration, scan-law interpolation or missing-line padding.
        """
        _channels((channel,))
        handler = _handler(result)
        info = _line_info(handler, channel)
        times = np.atleast_1d(get_cds_time(info["acq_time"]["Days"], info["acq_time"]["Milliseconds"]))
        ds = xr.Dataset({"time": ("row", times), **{name: ("row", info[name]) for name in
                        ("chan_id", "line_validity", "line_rquality", "line_gquality")}}, coords={"row": info["lineno"]})
        ds.time.attrs = {"source_variable": "LineSideInfo/L10LineMeanAcquisitionTime", "timing_support": "provider mean acquisition time per line", "timezone": "UTC"}
        ds.row.attrs = {"source_variable": "LineSideInfo/LineNumberInVIS_IRGrid (HRV: HRV grid)", "row_convention": ROW_CONVENTION}
        ds.attrs = {**_facts(handler), "channel": channel, "grid": "HRV" if channel == "HRV" else "VIS/IR",
                    "row_convention": ROW_CONVENTION, "product_id": result.target.product_id,
                    "platform": result.target.platform, "service_coverage": result.target.service_coverage}
        return ds

    def read(self, result, *, bbox=None, channels=DEFAULT_CHANNELS, calibration="radiance"):
        """Read explicit counts/radiance in a native window, without resampling.

        bbox selects the smallest rectangle containing native pixel centres inside
        it; outside returns empty images. HRV retains separate grid/window geometry.
        """
        if calibration not in ("counts", "radiance"):
            raise ValueError("calibration must be counts or radiance")
        bbox, channels = _bbox(bbox), _channels(channels)
        handler, output = _handler(result), xr.Dataset()
        facts = _facts(handler)
        for channel in channels:
            key = _key(channel, calibration)
            area = handler.get_area_def(key)
            rows, cols = _window(area, bbox)
            units = "count" if calibration == "counts" else "mW m-2 sr-1 (cm-1)-1"
            values = handler.get_dataset(key, {"units": units, "wavelength": None,
                                                 "standard_name": "counts" if calibration == "counts" else "radiance"})
            info = _line_info(handler, channel)
            if len(info["lineno"]) != values.sizes["y"] or area.shape != values.shape:
                raise ValueError("native line side info, area and image shape disagree")
            yd, xd = f"y_{channel}", f"x_{channel}"
            times = np.asarray(values.acq_time)[rows]
            values = values.drop_vars("acq_time").isel(y=rows, x=cols).compute().rename({"y": yd, "x": xd})
            index = CHANNELS.index(channel)
            coefficients = handler.header["15_DATA_HEADER"]["RadiometricProcessing"]["Level15ImageCalibration"]
            values.attrs = {"units": units, "platform": result.target.platform, "calibration": calibration,
                            "calibration_mode": "nominal", "coefficient_source": "native header RadiometricProcessing/Level15ImageCalibration/CalSlope, CalOffset",
                            "CalSlope": coefficients["CalSlope"][index], "CalOffset": coefficients["CalOffset"][index],
                            "row_convention": ROW_CONVENTION, "grid": "HRV" if channel == "HRV" else "VIS/IR",
                            "orbital_parameters": facts["orbital_parameters"],
                            "reader_provenance": {"reader": "seviri_l1b_native", "version": satpy.__version__,
                                "calib_mode": "nominal", "fill_disk": False, "external_coefficients": False,
                                "transforms": "10-bit unpacking; zero-count missing mask; nominal header affine calibration for radiance; CDS time decoding; header grid geometry including TypeOfEarthModel georeferencing offset and mean polar radius",
                                "negative_radiance_clipping": False, "line_quality_mask": False,
                                "reflectance": False, "brightness_temperature": False, "earth_sun_distance_correction": False,
                                "resampling": False, "padding": False}}
            px, py = _projected_coords(area, rows, cols)
            chunk = xr.Dataset({channel: values, f"{channel}_time": (yd, times),
                **{f"{channel}_{name}": (yd, info[name][rows]) for name in ("line_validity", "line_rquality", "line_gquality")},
                f"{channel}_projection_x": ((yd, xd), px), f"{channel}_projection_y": ((yd, xd), py)},
                coords={yd: info["lineno"][rows], xd: np.arange(cols.start, cols.stop)})
            chunk[f"{channel}_time"].attrs = {"source_variable": "LineSideInfo/L10LineMeanAcquisitionTime", "timezone": "UTC", "timing_support": "provider mean acquisition time per line"}
            chunk[yd].attrs = {"row_convention": ROW_CONVENTION, "source_variable": "LineSideInfo/LineNumberInVIS_IRGrid (HRV: HRV grid)"}
            for axis in ("x", "y"):
                chunk[f"{channel}_projection_{axis}"].attrs = {"units": "m", "source": "Satpy area from native reference grid and EarthModel"}
            chunk[channel].attrs["native_area"] = [{"crs": part.crs.to_string(), "area_extent": part.area_extent, "shape": part.shape} for part in _areas(area)]
            chunk[channel].attrs["native_window"] = {"rows": (rows.start, rows.stop), "columns": (cols.start, cols.stop)}
            output = xr.merge([output, chunk], join="exact")
        output.attrs = {**facts, "product_id": result.target.product_id, "collection": result.target.collection,
                        "platform": result.target.platform, "source_url": result.target.source_url,
                        "retrieved_at": result.retrieved_at.isoformat(), "integration_start": result.target.sensing_start.isoformat(),
                        "integration_end": result.target.sensing_end.isoformat(), "service_coverage": result.target.service_coverage,
                        "requested_bbox": bbox, "native_geometry": True, "resampled": False,
                        "row_convention": ROW_CONVENTION, "image_variables": list(channels)}
        return output

    def parse(self, result, **kwargs):
        return self.parse_canonical(result, **kwargs)

    def parse_canonical(self, result, *, bbox=None, channels=DEFAULT_CHANNELS, calibration="radiance"):
        ds = self.read(result, bbox=bbox, channels=channels, calibration=calibration)
        if not any(ds[channel].size for channel in ds.attrs["image_variables"]):
            return frame_from_records([])
        return target_to_canonical(result.target, retrieved_at=result.retrieved_at, qa=ds.attrs["provider_product_qa"])


def target_to_canonical(target, *, retrieved_at=None, qa=None):
    info = target.raw.get("properties", {}).get("productInformation", {})
    row = dict(time=target.sensing_start, valid_time=target.sensing_start,
               integration_start=target.sensing_start, integration_end=target.sensing_end,
               footprint_geometry=None, support_kind="swath", quantity="granule", value=None,
               unc_value=None, unc_status="unknown", platform=target.platform, instrument="SEVIRI",
               source="seviri_eumetsat", source_agency="EUMETSAT", source_url=target.source_url,
               retrieved_at=retrieved_at or target.retrieved_at, product_id=target.product_id,
               collection=target.collection, service=target.service, service_coverage=target.service_coverage)
    if info.get("processingVersion"):
        row["algorithm_version"] = str(info["processingVersion"])
    if info.get("version"):
        row["collection_version"] = str(info["version"])
    if qa is not None:
        row["qa"] = deepcopy(qa)
    elif info.get("qualityInformation"):
        row["qa"] = deepcopy(info["qualityInformation"])
    return frame_from_records([row])
