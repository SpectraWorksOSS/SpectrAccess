"""FCI repeat cycles, provider radiances and cloud products on native grids."""
from __future__ import annotations

import io
import itertools
import logging
import os
import re
import shutil
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

import numpy as np
import xarray as xr

from spectraccess.core.connector import Connector
from spectraccess.core.credentials import CredentialSource, CredentialRejected, provider_error, resolve
from spectraccess.core.schema import frame_from_records

try:
    import eumdac
    import hdf5plugin  # Registers the provider's FCIDECOMP (JPEG-LS) HDF5 filter.
    # NetCDF4 can use a separate HDF5 library from h5py (notably in wheels).
    # Preserve caller plugin directories while exposing the bundled filter there.
    _plugin_paths = os.environ.get("HDF5_PLUGIN_PATH", "").split(os.pathsep)
    if hdf5plugin.PLUGIN_PATH not in _plugin_paths:
        os.environ["HDF5_PLUGIN_PATH"] = os.pathsep.join(
            [path for path in _plugin_paths if path] + [hdf5plugin.PLUGIN_PATH])
    import h5py
    import h5netcdf
    import satpy
    from pyproj import CRS, Transformer
    from satpy.dataset import DataID
    from satpy.readers.fci_l1c_nc import FCIL1cNCFileHandler
    from satpy.readers.fci_l2_nc import FciL2NCFileHandler
except ImportError as exc:
    raise ImportError("FCIConnector requires pip install 'spectraccess[fci]' (Python >=3.11)") from exc

COLLECTIONS = {"l1c": "EO:EUM:DAT:0662", "cloud_mask": "EO:EUM:DAT:0678",
               "cloud_type": "EO:EUM:DAT:0680", "ctth": "EO:EUM:DAT:0681"}
CHANNELS = ("vis_04", "vis_05", "vis_06", "vis_08", "vis_09", "nir_13", "nir_16", "nir_22",
            "ir_38", "wv_63", "wv_73", "ir_87", "ir_97", "ir_105", "ir_123", "ir_133")
NOISE_DEFINITION = ("Provider radiometric noise model lookup table: radiometric_noise_lut_noise "
                    "at the effective radiances in radiometric_noise_lut_radiance; "
                    "not a per-pixel standard uncertainty or a coverage factor.")
_SUFFIX = re.compile(r"_(\d{14})_(\d{14})_([^_]+)_([^_]*)_([^_]+)_(\d{4})_(\d{4})\.nc$")
_QA_FIELDS = ("product_quality", "product_completeness", "product_timeliness")


class _EumdacRedaction(logging.Filter):
    """Sanitize upstream token and request records before any handler sees them."""
    def filter(self, record):
        message = record.getMessage()
        # EUMDAC token renewal records contain both current and previous tokens.
        if record.module == "token" and any(text in message for text in (
                "Current token ", "starting renewal of ", "Received/previous ",
                "Could not get fresh token from server")):
            message = "EUMDAC token status: <redacted>"
        message = re.sub(r"(?i)(Bearer\s+)[^\s'\"},]+", r"\1<redacted>", message)
        message = re.sub(r"(?i)(access_token=)[^&\s'\"]+", r"\1<redacted>", message)
        record.msg, record.args = message, ()
        return True


def _install_log_redaction():
    # token.py and request.py both emit on this same upstream logger.
    logger = logging.getLogger("eumdac")
    if not any(isinstance(item, _EumdacRedaction) for item in logger.filters):
        logger.addFilter(_EumdacRedaction())


_install_log_redaction()


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bbox(value):
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
class FCITarget:
    product_id: str
    collection: str
    title: str
    source_url: str
    retrieved_at: datetime
    raw: Mapping[str, Any]
    bbox: tuple[float, float, float, float]
    start: datetime
    end: datetime

    @property
    def footprint(self):
        geometry = self.raw.get("geometry")
        return deepcopy(geometry) if geometry and geometry.get("type") else None

    @property
    def coverage(self):
        return deepcopy([acquisition.get("acquisitionParameters", {}).get("mtgCoverage", {})
                         for acquisition in self.raw.get("properties", {}).get("acquisitionInformation", [])])

    @property
    def time_window(self):
        return {"start": self.start, "end": self.end}

    @property
    def product_version(self):
        return self.raw.get("download_properties", {}).get("productInformation", {}).get("productVersion")


@dataclass(frozen=True)
class FCIResult:
    path: Path
    target: FCITarget
    retrieved_at: datetime
    bbox: tuple[float, float, float, float]
    entries: tuple[str, ...]
    local_files: Mapping[str, str] = field(default_factory=dict)


def _path(result, entry):
    return result.path / result.local_files.get(entry, entry)


def _provider_failure(exc, stage):
    status = (getattr(exc, "extra_info", None) or {}).get("status")
    if status in (401, 403):
        return CredentialRejected("eumetsat", "credential source")
    detail = f" (HTTP {status})" if isinstance(status, int) else ""
    return RuntimeError(f"FCI EUMETSAT {stage} failed{detail}")


class _EntryRange(io.RawIOBase):
    """Seekable HDF5 metadata access using EUMDAC's maintained byte-range API.

    The HDF5 superblock publishes the EOF address. Small blocks are cached;
    pixel arrays are never accessed while choosing entries. A server that
    ignores Range is rejected rather than downloading a complete repeat cycle.
    """
    block_size = 65536

    def __init__(self, product, entry):
        self.product, self.entry, self.position = product, entry, 0
        self.blocks = {}
        header = self._range(0, 64)
        if header[:8] != b"\x89HDF\r\n\x1a\n":
            raise ValueError("FCI entry must be an HDF5 NetCDF file with a zero-offset superblock")
        version = header[8]
        if version in (2, 3):
            size, offset = header[9], 12
        elif version in (0, 1):
            size, offset = header[13], 24 if version == 0 else 28
        else:
            raise ValueError("unsupported HDF5 superblock version")
        self.length = int.from_bytes(header[offset + 2 * size:offset + 3 * size], "little")
        if size not in (2, 4, 8) or self.length < 64:
            raise ValueError("invalid HDF5 EOF address")

    def _range(self, start, end):
        with self.product.open(entry=self.entry, chunk=(start, end)) as stream:
            data = stream.read(end - start + 1)
        if len(data) != end - start:
            raise RuntimeError("EUMETSAT entry byte-range response has unexpected length")
        return data

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.length)
        if position < 0:
            raise ValueError("negative seek")
        self.position = position
        return position

    def read(self, size=-1):
        end = self.length if size < 0 else min(self.length, self.position + size)
        parts = []
        while self.position < end:
            block = self.position // self.block_size
            start = block * self.block_size
            if block not in self.blocks:
                self.blocks[block] = self._range(start, min(start + self.block_size, self.length))
            stop = min(end, start + len(self.blocks[block]))
            parts.append(self.blocks[block][self.position - start:stop - start])
            self.position = stop
        return b"".join(parts)

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


def _projection(attrs):
    return CRS.from_dict({"proj": "geos", "a": float(attrs["semi_major_axis"]),
                          "rf": float(attrs["inverse_flattening"]),
                          "h": float(attrs["perspective_point_height"]),
                          "lon_0": float(attrs["longitude_of_projection_origin"]),
                          "sweep": attrs.get("sweep_angle_axis", "y"), "units": "m"})


def _angles(variable):
    return np.asarray(variable[:], dtype=float) * variable.attrs.get("scale_factor", 1) + variable.attrs.get("add_offset", 0)


def _window(x, y, projection, bbox):
    """Smallest rectangular native window of pixel centres inside the bbox.

    Iterate rows to bound memory even for full-disc L2. Geographic evaluation
    uses provider scan angles and ellipsoid; no image resampling is involved.
    """
    west, south, east, north = _bbox(bbox)
    height = float(projection["perspective_point_height"])
    inverse = Transformer.from_crs(_projection(projection), "EPSG:4326", always_xy=True)
    rows, cols = [], []
    # Both provider L1c and L2 use west-positive x viewing angles.
    for row, angle in enumerate(y):
        lon, lat = inverse.transform(-x * height, np.full(x.shape, angle * height))
        inside = np.isfinite(lon) & np.isfinite(lat) & (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)
        selected = np.flatnonzero(inside)
        if selected.size:
            rows.append(row)
            cols.extend((selected[0], selected[-1]))
    if not rows:
        return slice(0, 0), slice(0, 0)
    return slice(rows[0], rows[-1] + 1), slice(min(cols), max(cols) + 1)


def _entry_intersects(product, entry, bbox, channels):
    with _EntryRange(product, entry) as stream, h5netcdf.File(stream, "r") as nc:
        data = nc.groups["data"]
        projection = dict(data.variables["mtg_geos_projection"].attrs)
        for channel in channels:
            if channel not in data.groups:
                continue
            group = data.groups[channel].groups["measured"]
            rows, cols = _window(_angles(group.variables["x"]), _angles(group.variables["y"]), projection, bbox)
            if rows.stop and cols.stop:
                return True
    return False


def _file_info(path):
    match = _SUFFIX.search(path.name)
    if match is None or "-FDHSI-FD-" not in path.name:
        raise ValueError("unsupported FCI L1c filename; expected FDHSI full-disc body chunk")
    prefix = path.name[:match.start()]
    # The final prefix fields are processing_time, facility_or_tool, environment.
    return {"start_time": datetime.strptime(match[1], "%Y%m%d%H%M%S"),
            "end_time": datetime.strptime(match[2], "%Y%m%d%H%M%S"),
            "coverage": "FD", "facility_or_tool": prefix.split("_")[-2],
            "repeat_cycle_in_day": int(match[6]), "count_in_repeat_cycle": int(match[7])}


def _key(name, resolution, calibration=None):
    from satpy.dataset.dataid import default_id_keys_config
    values = {"name": name, "resolution": resolution}
    if calibration:
        values["calibration"] = calibration
    return DataID(default_id_keys_config, **values)


def _l1_handler(path, entry=None, *, clip_negative_radiances=False):
    handler = FCIL1cNCFileHandler(str(path), _file_info(Path(entry) if entry else path),
                                 {"file_type": "fci_l1c_fdhsi"},
                                 clip_negative_radiances=clip_negative_radiances)
    # The standalone NetCDF inspector stores global attrs under /attr/;
    # FCI's YAML-listed-variable path uses attr/. Supply the same source key.
    handler.file_content["attr/platform"] = handler.file_content["/attr/platform"]
    return handler


def _close_l1(handler):
    if handler.file_handle is not None:
        handle, handler.file_handle = handler.file_handle, None
        handle.close()


@contextmanager
def _managed_l1(path, entry):
    handler = _l1_handler(path, entry)
    try:
        yield handler
    finally:
        _close_l1(handler)


def _source_array(var, name, group, path, slices=None, dimensions=None):
    index = tuple((slices or {}).get(dim, slice(None)) for dim in var.dimensions)
    values = np.asarray(var[index] if var.dimensions else var[()])
    attrs = {key: deepcopy(value) for key, value in var.attrs.items()}
    attrs.update(source_variable=f"{group}/{name}", source_file=path.name)
    enum = h5py.check_dtype(enum=var.dtype)
    if enum:
        attrs["flag_values"] = list(enum.values())
        attrs["flag_meanings"] = " ".join(enum)
    dims = tuple((dimensions or {}).get(dim, f"{path.stem}_{group.replace('/', '_')}_{dim}") for dim in var.dimensions)
    return xr.DataArray(values, dims=dims, attrs=attrs)


class FCIConnector(Connector):
    """EUMDAC discovery and chunk fetch, Satpy values, native-window Dataset."""
    credential_provider = "eumetsat"

    def __init__(self, *, credentials: CredentialSource | None = None):
        self.credentials = credentials

    def _store(self, credentials=None):
        credential = resolve("eumetsat", credentials if credentials is not None else self.credentials)
        try:
            token = eumdac.AccessToken((credential.account, credential.secret))
            # Force authentication here so errors cannot expose consumer credentials.
            str(token)
            return eumdac.DataStore(token)
        except Exception as exc:
            failure = provider_error("eumetsat", credentials if credentials is not None else self.credentials,
                                     exc, credential, RuntimeError)
            if isinstance(failure, CredentialRejected):
                raise failure from None
            raise _provider_failure(exc, "token authentication") from None

    def discover(self, *, bbox, start: date | datetime, end: date | datetime,
                 products=("l1c", "cloud_mask", "cloud_type", "ctth"), limit=10):
        bbox, start, end = _bbox(bbox), _utc(start), _utc(end)
        products = tuple(products)
        if not products or set(products) - set(COLLECTIONS):
            raise ValueError(f"products must select from {tuple(COLLECTIONS)}")
        if limit < 0 or end < start:
            raise ValueError("limit must be >=0 and end must be >= start")
        if limit == 0:
            return []
        store, targets = self._store(), []
        try:
            for name in products:
                collection = COLLECTIONS[name]
                found = store.get_collection(collection).search(dtstart=start, dtend=end, bbox=",".join(map(str, bbox)))
                for product in itertools.islice(found, limit):
                    raw = deepcopy(product.metadata)
                    identifier = str(product)
                    url = (f"https://api.eumetsat.int/data/download/1.0.0/collections/{quote(collection, safe='')}"
                           f"/products/{quote(identifier, safe='')}/metadata?format=json")
                    targets.append(FCITarget(identifier, collection, identifier, url, datetime.now(timezone.utc),
                                             raw, bbox, _utc(product.sensing_start), _utc(product.sensing_end)))
        except Exception as exc:
            raise _provider_failure(exc, "catalogue query") from None
        return targets

    def fetch(self, target, *, dest=None, bbox=None, channels=CHANNELS, credentials=None):
        channels = _channels(channels)
        bbox = _bbox(target.bbox if bbox is None else bbox)
        store = self._store(credentials)
        path = Path(dest) if dest is not None else Path("data/fci")
        # Provider identifier can contain slashes; local folder uses a stable digest.
        import hashlib
        path = path / hashlib.sha256((target.collection + target.product_id).encode()).hexdigest()[:20]
        path.mkdir(parents=True, exist_ok=True)
        downloaded, local_files = [], {}
        try:
            product = store.get_product(target.collection, target.product_id)
            available = tuple(product.entries)
            l1c = target.collection == COLLECTIONS["l1c"]
            selected = []
            for entry in available:
                if not entry.endswith(".nc"):
                    continue
                if not l1c or "-TRAIL-" in entry:
                    selected.append(entry)
                elif "-BODY-" in entry and _entry_intersects(product, entry, bbox, channels):
                    selected.append(entry)
            if l1c and any("-BODY-" in e for e in selected) and not any("-TRAIL-" in e for e in selected):
                raise ValueError("FCI repeat cycle has no trailer entry")
            for entry in selected:
                filename = Path(entry.replace("\\", "/")).name
                # WMO filenames plus temporary/cache roots can exceed Windows'
                # NetCDF library path limit. Preserve source names in the result.
                local_name = hashlib.sha256(entry.encode()).hexdigest()[:20] + ".nc"
                output = path / local_name
                partial = output.with_suffix(output.suffix + ".part")
                try:
                    with product.open(entry=entry) as stream, partial.open("wb") as sink:
                        shutil.copyfileobj(stream, sink)
                    partial.replace(output)
                finally:
                    partial.unlink(missing_ok=True)
                downloaded.append(filename)
                local_files[filename] = local_name
        except Exception as exc:
            raise _provider_failure(exc, "entry fetch") from None
        return FCIResult(path, target, datetime.now(timezone.utc), bbox, tuple(downloaded), local_files)

    def parse(self, result, **kwargs):
        return self.parse_canonical(result, **kwargs)

    def parse_canonical(self, result, *, bbox=None, channels=CHANNELS):
        ds = self.read(result, bbox=bbox, channels=channels)
        if not any(ds[name].size for name in ds.attrs["image_variables"]):
            return frame_from_records([])
        frame = target_to_canonical(result.target, retrieved_at=result.retrieved_at,
                                    provider_attrs=ds.attrs.get("provider_attrs", {}),
                                    qa=ds.attrs.get("provider_product_qa"))
        if any("radiometric_noise_lut_noise" in name for name in ds):
            frame["unc_definition"] = NOISE_DEFINITION
        return frame

    def read(self, result, *, bbox=None, channels=CHANNELS):
        bbox = _bbox(result.bbox if bbox is None else bbox)
        channels = _channels(channels)
        if result.target.collection == COLLECTIONS["l1c"]:
            dataset = _read_l1(result, bbox, channels)
        else:
            dataset = _read_l2(result, bbox)
        dataset.attrs.update(product_id=result.target.product_id, collection=result.target.collection,
                             source_url=result.target.source_url, retrieved_at=result.retrieved_at.isoformat(),
                             integration_start=result.target.start.isoformat(),
                             integration_end=result.target.end.isoformat(),
                             fetched_bbox=result.bbox, requested_bbox=bbox, source_entries=result.entries,
                             provider_coverage=result.target.coverage)
        if result.target.footprint:
            dataset.attrs["footprint_geometry"] = result.target.footprint
        return dataset


def target_to_canonical(target, *, retrieved_at=None, provider_attrs=None, qa=None):
    attrs = provider_attrs or {}
    props = target.raw.get("properties", {})
    product_info = {**props.get("productInformation", {}),
                    **target.raw.get("download_properties", {}).get("productInformation", {})}
    row = dict(time=target.start, valid_time=target.start, integration_start=target.start,
               integration_end=target.end, footprint_geometry=target.footprint, support_kind="swath",
               quantity="granule", value=None, unc_value=None, unc_status="unknown",
               platform=attrs.get("platform"), instrument="FCI", source="fci_eumetsat",
               source_agency="EUMETSAT", source_url=target.source_url,
               retrieved_at=retrieved_at or target.retrieved_at,
               product_id=target.product_id, collection=target.collection,
               provider_coverage=target.coverage, product_version=product_info.get("productVersion"))
    if row["platform"] is None:
        acquisitions = props.get("acquisitionInformation", [])
        if acquisitions:
            row["platform"] = acquisitions[0].get("platform", {}).get("platformShortName")
    version = attrs.get("processor_version") or product_info.get("processingInformation", {}).get("processorVersion") or product_info.get("processingVersion")
    release = attrs.get("release_version") or attrs.get("baseline_version") or product_info.get("version")
    if version:
        row["algorithm_version"] = str(version)
    if release:
        row["collection_version"] = str(release)
    quality = {**product_info.get("qualityInformation", {}), **(qa or {})}
    if quality:
        row["qa"] = deepcopy(quality)
    return frame_from_records([row])


def _read_l1(result, bbox, channels):
    pieces = {channel: [] for channel in channels}
    ancillary, provider_attrs, qa = {}, {}, {}
    files = [name for name in result.entries if "-BODY-" in name]
    windows, columns = {}, {c: [] for c in channels}
    for entry in files:
        with h5netcdf.File(_path(result, entry), "r") as nc:
            data = nc.groups["data"]
            projection = dict(data.variables["mtg_geos_projection"].attrs)
            for channel in channels:
                if channel not in data.groups:
                    raise ValueError(f"FCI body entry lacks requested channel {channel}")
                group = data.groups[channel].groups["measured"]
                x, y = _angles(group.variables["x"]), _angles(group.variables["y"])
                rows, cols = _window(x, y, projection, bbox)
                windows[entry, channel] = rows
                if rows.stop:
                    columns[channel].extend(x[cols])
    for entry in files:
        path = _path(result, entry)
        with _managed_l1(path, entry) as handler, h5netcdf.File(path, "r") as nc:
            provider_attrs[entry] = dict(nc.attrs)
            data = nc.groups["data"]
            projection = dict(data.variables["mtg_geos_projection"].attrs)
            for channel in channels:
                if channel not in data.groups:
                    raise ValueError(f"FCI body entry lacks requested channel {channel}")
                group = data.groups[channel].groups["measured"]
                x, y = _angles(group.variables["x"]), _angles(group.variables["y"])
                rows = windows[entry, channel]
                if rows.stop == 0:
                    continue
                column_positions = np.flatnonzero((x >= min(columns[channel])) & (x <= max(columns[channel])))
                cols = slice(column_positions[0], column_positions[-1] + 1)
                yd, xd = f"y_{channel}", f"x_{channel}"
                grid = {"y": yd, "x": xd}
                source = f"data/{channel}/measured"
                for variable_name, var in group.variables.items():
                    if variable_name in ("effective_radiance", "pixel_quality", "index_map", "x", "y"):
                        continue
                    public_name = f"{channel}_{variable_name}_{_file_info(Path(entry))['count_in_repeat_cycle']:04d}"
                    ancillary[public_name] = _source_array(var, variable_name, source, path)
                    ancillary[public_name].attrs["source_file"] = entry
                key = _key(channel, 1000 if channel.startswith(("vis", "nir")) else 2000, "radiance")
                values = handler.get_dataset(key, {"units": group.variables["effective_radiance"].attrs["units"]})
                values = values.isel(y=rows, x=cols).compute().rename(grid)
                raw_attrs = dict(group.variables["effective_radiance"].attrs)
                values.attrs = {"provider_attrs": raw_attrs, "units": raw_attrs["units"],
                                "source_variable": f"{source}/effective_radiance", "source_file": entry,
                                "reader_provenance": {"reader": "fci_l1c_nc", "version": satpy.__version__,
                                    "calibration": "radiance", "clip_negative_radiances": handler.clip_negative_radiances,
                                    "global_clip_negative_radiances": satpy.config.get("readers.clip_negative_radiances"),
                                    "transform": "provider scale/offset (dual gain for ir_38); provider valid-range/fill mask"}}
                quality = _source_array(group.variables["pixel_quality"], "pixel_quality", source, path,
                                        {"y": rows, "x": cols}, grid)
                quality.attrs["source_file"] = entry
                index_var = group.variables["index_map"]
                indices = np.asarray(index_var[rows, cols], dtype=np.int64)
                indices = indices - int(np.min(nc.variables["index"][:]))
                time_var = nc.variables["time"]
                lut = np.asarray(time_var[:])
                valid = (indices >= 0) & (indices < len(lut))
                if "_FillValue" in index_var.attrs:
                    valid &= np.asarray(index_var[rows, cols]) != index_var.attrs["_FillValue"]
                times = np.full(indices.shape, np.nan)
                times[valid] = lut[indices[valid]]
                if "_FillValue" in time_var.attrs:
                    times[times == time_var.attrs["_FillValue"]] = np.nan
                time = xr.DataArray(times, dims=(yd, xd), attrs={**dict(time_var.attrs),
                    "source_variable": "time", "index_variable": f"{source}/index_map", "source_file": entry,
                    "timing_support": "provider pixel acquisition time"})
                # Provider scan-angle coordinates retain original orientation and attrs.
                chunk = xr.Dataset({channel: values, f"{channel}_pixel_quality": quality,
                                    f"{channel}_time": time}, coords={yd: y[rows], xd: x[cols]})
                for dim, var in [(yd, "y"), (xd, "x")]:
                    chunk[dim].attrs = {"provider_attrs": dict(group.variables[var].attrs),
                                        "units": "rad", "source_variable": f"{source}/{var}"}
                chunk.attrs["projection"] = projection
                pieces[channel].append(chunk)
    output = xr.Dataset()
    for channel, chunks in pieces.items():
        if chunks:
            merged = xr.concat(chunks, dim=f"y_{channel}", join="exact", combine_attrs="drop_conflicts")
            # Concatenation contains only existing segment rows; gaps are never padded.
            if len(np.unique(merged[f"y_{channel}"])) != merged.sizes[f"y_{channel}"]:
                raise ValueError("overlapping FCI body rows in repeat cycle")
            output = xr.merge([output, merged], join="exact", compat="no_conflicts")
            output[channel].attrs["provider_projection"] = chunks[0].attrs["projection"]
    for name in result.entries:
        if "-TRAIL-" not in name:
            continue
        path = _path(result, name)
        with h5netcdf.File(path, "r") as nc:
            provider_attrs[name] = dict(nc.attrs)
            def walk(group, prefix=""):
                for variable_name, var in group.variables.items():
                    if prefix.startswith("data/") and ("radiometric_noise_lut" in variable_name or "quality" in prefix):
                        public_name = prefix.replace("/", "_") + "_" + variable_name
                        ancillary[public_name] = _source_array(var, variable_name, prefix, path)
                        ancillary[public_name].attrs["source_file"] = name
                        if "quality" in prefix:
                            qa[f"{prefix}/{variable_name}"] = {"value": np.asarray(var[()]).tolist(), "attrs": dict(var.attrs)}
                for group_name, child in group.groups.items():
                    walk(child, f"{prefix}/{group_name}".strip("/"))
            walk(nc)
    output.update(ancillary)
    roots = next(iter(provider_attrs.values()), {})
    output.attrs = {"provider_attrs": roots, "provider_source_attrs": provider_attrs,
                    "provider_product_qa": qa, "native_geometry": True,
                    "pixel_time_available": True, "segment_padding": False,
                    "image_variables": [c for c in channels if c in output]}
    return output


def _read_l2(result, bbox):
    output, provider_attrs, qa, image_variables = xr.Dataset(), {}, {}, []
    for name in result.entries:
        path = _path(result, name)
        handler = FciL2NCFileHandler(str(path), {"start_time": result.target.start,
            "end_time": result.target.end, "spacecraft_id": 1}, {"file_type": "nc_fci_clm"})
        with h5netcdf.File(path, "r") as nc:
            provider_attrs[name] = dict(nc.attrs)
            projection = dict(nc.variables["mtg_geos_projection"].attrs)
            rows, cols = _window(_angles(nc.variables["x"]), _angles(nc.variables["y"]), projection, bbox)
            grid = {"number_of_rows": f"y_{path.stem}", "number_of_columns": f"x_{path.stem}"}
            slices = {"number_of_rows": rows, "number_of_columns": cols}
            for variable_name, var in nc.variables.items():
                if variable_name in ("x", "y", "mtg_geos_projection"):
                    continue
                if variable_name in _QA_FIELDS:
                    qa[variable_name] = {"value": np.asarray(var[()]).tolist(), "attrs": dict(var.attrs)}
                image = "number_of_rows" in var.dimensions and "number_of_columns" in var.dimensions
                if image:
                    image_variables.append(variable_name)
                flags = (h5py.check_dtype(enum=var.dtype) is not None or "flag_meanings" in var.attrs or "flag_values" in var.attrs
                         or "flag_masks" in var.attrs or "quality" in variable_name)
                if image and not flags:
                    array = handler.get_dataset(_key(variable_name, 2000), {"nc_key": variable_name,
                        "name": variable_name, "file_type": "nc_fci_clm"})
                    if array is None:
                        raise ValueError(f"Satpy did not read provider variable {variable_name}")
                    array = array.isel(y=rows, x=cols).compute()
                    dimensions = {"y": grid["number_of_rows"], "x": grid["number_of_columns"]}
                    dimensions.update({d: f"{path.stem}_{variable_name}_{d}" for d in array.dims if d not in dimensions})
                    array = array.rename(dimensions)
                    array.attrs = {"provider_attrs": dict(var.attrs), "source_file": name,
                        "source_variable": variable_name, "provider_projection": projection,
                        "reader_provenance": {"reader": "fci_l2_nc", "version": satpy.__version__,
                                               "transform": "provider CF scale/offset and fill mask"}}
                else:
                    array = _source_array(var, variable_name, "", path, slices, grid)
                    array.attrs["source_file"] = name
                    if image:
                        array.attrs["provider_projection"] = projection
                output[variable_name] = array
            for axis, dim, window in [("y", "number_of_rows", rows), ("x", "number_of_columns", cols)]:
                if grid[dim] in output.dims:
                    output = output.assign_coords({grid[dim]: _angles(nc.variables[axis])[window]})
                    output[grid[dim]].attrs = {"units": "rad", "source_file": name,
                        "source_variable": axis, "provider_attrs": dict(nc.variables[axis].attrs)}
        handler.nc.close()
    output.attrs = {"provider_attrs": next(iter(provider_attrs.values()), {}),
                    "provider_source_attrs": provider_attrs, "provider_product_qa": qa,
                    "native_geometry": True, "pixel_time_available": False,
                    "image_variables": image_variables}
    return output
