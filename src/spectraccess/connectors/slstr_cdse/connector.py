"""SL_1_RBT provider measurements, with Satpy calibration adjustments disabled.

File layout: ESA SLSTR Level-1 Product Data Format Specification, issue 2.12.
Each stripe/view keeps independent dimensions. Tie-point geometry stays on its
published grid; no interpolation, reprojection or resampling is performed.
"""
from __future__ import annotations

import itertools
import json
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from xml.etree import ElementTree as ET

import numpy as np
import xarray as xr

from spectraccess.core.connector import Connector
from spectraccess.core.credentials import CredentialSource, resolve, provider_error
from spectraccess.core.schema import frame_from_records
from spectraccess.connectors.sentinel2_cdse.connector import _CaptureLogger, _ExplicitCredentials, _as_utc, _bbox_to_wkt

try:
    from cdsetool.query import query_features
    from cdsetool.download import download_feature, download_file
    from satpy.readers.slstr_l1b import NCSLSTR1B
except ImportError as exc:
    raise ImportError("SLSTRConnector requires pip install 'spectraccess[slstr]' (Python >=3.11)") from exc

PRODUCT_TYPE = "SL_1_RBT___"
CHANNELS = tuple([f"S{i}" for i in range(1, 10)] + ["F1", "F2"])
UNITY = {f"{channel}_{view}": 1.0 for channel in CHANNELS for view in ("nadir", "oblique")}
PRODUCT_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})"
_MEASUREMENT = re.compile(r"(S[1-9]|F[12])_(radiance|BT)_([abif])([no])\.nc$")


@dataclass(frozen=True)
class SLSTRTarget:
    product_id: str
    title: str
    source_url: str
    retrieved_at: datetime
    raw: Mapping[str, Any]

    @property
    def footprint(self):
        return deepcopy(self.raw.get("GeoFootprint"))

    @property
    def time_window(self):
        return deepcopy(self.raw.get("ContentDate"))


@dataclass(frozen=True)
class SLSTRResult:
    path: Path
    target: SLSTRTarget
    retrieved_at: datetime


def _selection(channels, views):
    channels, views = tuple(channels), tuple(views)
    if not channels or set(channels) - set(CHANNELS):
        raise ValueError(f"channels must select from {CHANNELS}")
    if not views or set(views) - {"nadir", "oblique"}:
        raise ValueError("views must select nadir and/or oblique")
    return channels, views


def _manifest(path):
    return ET.parse(path / "xfdumanifest.xml")


def _annotation_grid(grid):
    """PDFS: fire measurements use f; their row/quality support uses i."""
    return "i" + grid[1] if grid[0] == "f" else grid


def _assets(tree, channels, views):
    """Select actual published files, including ancillary uncertainty tables."""
    available = {Path(n.get("href", "")).name for n in tree.iter() if n.tag.split("}")[-1] == "fileLocation"}
    selected = set()
    grids = set()
    for name in available:
        match = _MEASUREMENT.fullmatch(name)
        if match and match[1] in channels and match[4] in {v[0] for v in views}:
            selected.add(name)
            grids.add(match[3] + match[4])
    for channel in channels:
        for view in views:
            if not any(n.startswith(channel + "_") and n.endswith(view[0] + ".nc") for n in selected):
                raise ValueError(f"manifest has no measurement for {channel} {view}")
    for grid in grids:
        for prefix in ("geodetic", "cartesian", "indices", "flags"):
            name = f"{prefix}_{grid}.nc"
            if name not in available:
                raise ValueError(f"manifest lacks {name}")
            selected.add(name)
    for name in ("geodetic_tx.nc", "cartesian_tx.nc", *(f"geometry_t{v[0]}.nc" for v in views)):
        if name not in available:
            raise ValueError(f"manifest lacks {name}")
        selected.add(name)
    annotation_grids = {_annotation_grid(g) for g in grids}
    for name in available:
        if (name == "viscal.nc" or name.startswith("time_")
                or name in {f"geometry_t{v[0]}.nc" for v in views}
                or any(name.startswith(c + "_") and ("quality" in name or "uncertainty" in name)
                       and name.rsplit("_", 1)[-1].removesuffix(".nc") in annotation_grids for c in channels)):
            selected.add(name)
    if "viscal.nc" not in selected or "time_in.nc" not in selected:
        raise ValueError("manifest lacks viscal.nc or time_in.nc")
    return sorted(selected)


class SLSTRConnector(Connector):
    """CDSE discovery, filtered SEN3 fetch, granule rows and native Dataset."""
    credential_provider = "cdse"

    def __init__(self, *, credentials: CredentialSource | None = None):
        self.credentials = credentials

    def discover(self, *, bbox, start: date | datetime, end: date | datetime,
                 products=(PRODUCT_TYPE,), limit=10):
        if tuple(products) != (PRODUCT_TYPE,):
            raise ValueError("only SL_1_RBT___ is supported")
        if limit < 0:
            raise ValueError("limit must be >=0")
        start, end = _as_utc(start), _as_utc(end)
        if end < start:
            raise ValueError("end must be >= start")
        geometry = _bbox_to_wkt(bbox)
        if limit == 0:
            return []
        terms = {"productType": PRODUCT_TYPE, "geometry": geometry,
                 "contentDateEndGe": start, "contentDateStartLe": end, "top": min(limit, 1000)}
        log = _CaptureLogger()
        try:
            features = list(itertools.islice(query_features("SENTINEL-3", terms,
                options={"logger": log, "expand_attributes": True}), limit))
        except Exception:
            raise RuntimeError("SLSTR CDSE catalogue query failed") from None
        if log.errors:
            raise RuntimeError("SLSTR CDSE catalogue reported a provider error")
        targets = []
        for feature in features:
            if "SL_1_RBT" not in feature["Name"]:
                raise ValueError("CDSE returned a product outside SL_1_RBT")
            window = feature["ContentDate"]
            if _as_utc(datetime.fromisoformat(window["End"].replace("Z", "+00:00"))) < start or _as_utc(datetime.fromisoformat(window["Start"].replace("Z", "+00:00"))) > end:
                continue
            raw = deepcopy(feature)
            raw["Collection"] = "SENTINEL-3"
            targets.append(SLSTRTarget(raw["Id"], raw["Name"], PRODUCT_URL.format(product_id=raw["Id"]),
                                       datetime.now(timezone.utc), raw))
        return targets

    def fetch(self, target, *, dest, channels=CHANNELS, views=("nadir", "oblique"), credentials=None):
        channels, views = _selection(channels, views)
        source = credentials if credentials is not None else self.credentials
        credential = resolve("cdse", source)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        log = _CaptureLogger()
        failure = None
        try:
            options = {"logger": log, "credentials": _ExplicitCredentials(credential.account, credential.secret), "download_attempts": 3}
            with tempfile.TemporaryDirectory(dir=dest) as tmp:
                staged = Path(tmp) / "staged"
                staged.mkdir()
                url = (f"https://download.dataspace.copernicus.eu/odata/v1/Products({target.product_id})"
                       f"/Nodes({target.title})/Nodes(xfdumanifest.xml)/$value")
                if not download_file(url, staged / "xfdumanifest.xml", options) or log.errors:
                    raise RuntimeError("SLSTR manifest download failed")
                files = _assets(_manifest(staged), channels, views)
                for filename in files:
                    asset_dest = Path(tmp) / filename
                    asset_dest.mkdir()
                    name = download_feature(deepcopy(dict(target.raw)), str(asset_dest),
                                            {**options, "filter_pattern": filename})
                    asset = asset_dest / name / filename if name else None
                    if log.errors or asset is None or not asset.is_file():
                        raise RuntimeError(f"SLSTR download missing required {filename}")
                    asset.replace(staged / filename)
                product = dest / target.title
                product.mkdir(exist_ok=True)
                for asset in staged.iterdir():
                    asset.replace(product / asset.name)
        except Exception as exc:
            failure = provider_error("cdse", source, exc, credential, RuntimeError)
        if failure is not None:
            raise failure from None
        return SLSTRResult(product, target, datetime.now(timezone.utc))

    def read(self, raw, *, bbox=None, channels=CHANNELS, views=("nadir", "oblique")):
        """Decoded radiance/BT, raw flags, row times and native tie-point angles.

        Dataset variable names retain provider names. Dimensions rows_<grid>,
        columns_<grid> prevent xarray aligning or resizing unlike native grids.
        """
        channels, views = _selection(channels, views)
        path = raw.path if isinstance(raw, SLSTRResult) else Path(raw)
        if bbox is not None:
            _bbox_to_wkt(bbox)
        output = xr.Dataset()
        windows = {}
        measurements = []
        for asset in sorted(path.glob("*.nc")):
            match = _MEASUREMENT.fullmatch(asset.name)
            if match and match[1] in channels and match[4] in {v[0] for v in views}:
                measurements.append((asset, match))
                grid = match[3] + match[4]
                if grid not in windows:
                    with xr.open_dataset(path / f"geodetic_{grid}.nc") as geo:
                        lon, lat = geo[f"longitude_{grid}"], geo[f"latitude_{grid}"]
                        mask = np.isfinite(lon.values) & np.isfinite(lat.values)
                        if bbox is not None:
                            mask &= (lon.values >= bbox[0]) & (lon.values <= bbox[2]) & (lat.values >= bbox[1]) & (lat.values <= bbox[3])
                        ii, jj = np.nonzero(mask)
                        window = {"rows": slice(int(ii.min()), int(ii.max()) + 1), "columns": slice(int(jj.min()), int(jj.max()) + 1)} if ii.size else {"rows": slice(0, 0), "columns": slice(0, 0)}
                        windows[grid] = window
                        _copy_native(output, geo, grid, window, asset=f"geodetic_{grid}.nc")
        for channel in channels:
            for view in views:
                if not any(m[1] == channel and m[4] == view[0] for _, m in measurements):
                    raise ValueError(f"missing measurement for {channel} {view}")
        for asset, match in measurements:
            channel, kind, stripe, view = match.groups()
            grid = stripe + view
            handler = NCSLSTR1B(str(asset), {"dataset_name": channel, "stripe": stripe, "view": view,
                               "mission_id": path.name[:3] if path.name[:3] in {"S3A", "S3B", "S3C", "S3D"} else "S3A"}, {}, user_calibration=UNITY)
            try:
                key = _ReaderKey(channel, stripe, view, "radiance" if kind == "radiance" else "brightness_temperature")
                values = handler.get_dataset(key, {}).rename({"y": "rows", "x": "columns"})
                name = f"{channel}_{kind}_{grid}"
                values = values.isel(windows[grid]).load()
                original = handler.nc[name].attrs.copy()
                packing = {k: handler.nc[name].encoding[k] for k in ("scale_factor", "add_offset", "_FillValue") if k in handler.nc[name].encoding}
                values.attrs = {**original, "provider_variable": name, "provider_file": asset.name,
                                "reader": "satpy.slstr_l1b", "reader_version": "0.60.0",
                                "calibration": key["calibration"], "radiance_adjustment_factor": 1.0,
                                "provider_packing": packing,
                                "value_transform": "provider CF scale/offset and fill decoding only"}
                output[name] = _native_array(values, grid, asset.name)
                # Radiance error arrays, orphan arrays and IR tables stay in their
                # own source dimensions. Never turn a table into pixel sigma.
                with xr.open_dataset(asset) as source, xr.open_dataset(asset, mask_and_scale=False) as encoded:
                    for variable in source.data_vars:
                        if variable == name:
                            continue
                        field = encoded[[variable]] if "exception" in variable or "flag" in variable else source[[variable]]
                        _copy_native(output, field, grid, windows[grid], asset=asset.name)
            finally:
                handler.nc.close()
                handler.cal.close()
                handler.indices.close()
        for grid, window in windows.items():
            with xr.open_dataset(path / f"flags_{grid}.nc", mask_and_scale=False) as flags:
                _copy_native(output, flags, grid, window, asset=f"flags_{grid}.nc")
            with xr.open_dataset(path / f"indices_{grid}.nc", mask_and_scale=False) as indices:
                _copy_native(output, indices, grid, window, asset=f"indices_{grid}.nc")
            with xr.open_dataset(path / f"cartesian_{grid}.nc") as cartesian:
                _copy_native(output, cartesian, grid, window, asset=f"cartesian_{grid}.nc")
        # Tie-point rows/columns are a separate published support, common to
        # both views. Return that support in full, without interpolation.
        for filename in ("geodetic_tx.nc", "cartesian_tx.nc"):
            with xr.open_dataset(path / filename) as support:
                _copy_native(output, support, "tx", None, asset=filename)
        for name in ("latitude_tx", "longitude_tx"):
            output.coords[name] = output[name]
        annotation_windows = dict(windows)
        for grid, window in windows.items():
            annotation_windows.setdefault(_annotation_grid(grid), {"rows": window["rows"]})
        for asset in sorted(path.glob("*.nc")):
            if asset.name.startswith("time_"):
                with xr.open_dataset(asset) as times:
                    for name, value in times.data_vars.items():
                        suffix = name.rsplit("_", 1)[-1]
                        grids = [suffix] if suffix in annotation_windows else [g for g in windows if _annotation_grid(g)[0] == suffix]
                        if name.startswith("time_stamp_"):
                            for grid in grids:
                                if value.dims != ("rows",) or value.size < windows[grid]["rows"].stop:
                                    raise ValueError(f"{name} row shape does not match {grid}")
                                stamp = _native_array(value.isel(rows=windows[grid]["rows"]), grid, asset.name)
                                if not np.issubdtype(stamp.dtype, np.datetime64):
                                    raise ValueError(f"{name} must decode to provider row times")
                                alias = f"time_stamp_{grid}"
                                output.coords[alias] = stamp
                                output[alias].attrs.update(provider_file=asset.name, provider_variable=name,
                                    provider_time_units=value.encoding.get("units"),
                                    time_support="sub-satellite image-row crossing; common to nadir/oblique, not exact pixel acquisition")
                        elif grids:
                            grid = grids[0]
                            _copy_native(output, times[[name]], grid, annotation_windows[grid], asset=asset.name)
                        elif value.ndim == 0 and name not in output:
                            output[name] = _native_array(value, asset.stem, asset.name)
            elif asset.name.startswith("geometry_t") and asset.name[-4] in {v[0] for v in views}:
                with xr.open_dataset(asset) as geometry:
                    _copy_native(output, geometry, "tx", None, asset=asset.name)
                    for name in geometry.data_vars:
                        output[name].attrs["spatial_support"] = "geodetic_tx.nc; cartesian_tx.nc"
            elif "quality" in asset.name or "uncertainty" in asset.name:
                grid = asset.stem.rsplit("_", 1)[-1]
                channel = asset.stem.split("_", 1)[0]
                if channel in channels and grid in annotation_windows:
                    with xr.open_dataset(asset) as quality, xr.open_dataset(asset, mask_and_scale=False) as encoded:
                        for name, value in quality.data_vars.items():
                            field = encoded[[name]] if "exception" in name or "flag" in name or "flag_masks" in value.attrs else quality[[name]]
                            _copy_native(output, field, grid, annotation_windows[grid], asset=asset.name)
        for grid in windows:
            if f"time_stamp_{grid}" not in output:
                raise ValueError(f"missing provider time_stamp_{grid}; no granule-time substitution")
            stamp = output[f"time_stamp_{grid}"]
            if stamp.size != output.sizes[f"rows_{grid}"]:
                raise ValueError(f"row time shape mismatch on {grid}")
        output.attrs.update(native_geometry=True, resampled=False, support_kind="pixel",
                            geometry_support="provider tie-point grid; not interpolated",
                            product_title=path.name)
        return output

    def parse(self, raw, **kwargs):
        return self.read(raw, **kwargs)

    def parse_canonical(self, raw, *, target=None, bbox=None):
        path = raw.path if isinstance(raw, SLSTRResult) else Path(raw)
        if isinstance(raw, SLSTRResult):
            target = raw.target
        tree = _manifest(path)
        metadata = {} if target is None else deepcopy(dict(target.raw))
        attrs = {a["Name"]: a["Value"] for a in metadata.get("Attributes", [])}
        window = metadata.get("ContentDate", {})
        provider = {}
        qa = {}
        version = None
        for node in tree.iter():
            local = node.tag.split("}")[-1]
            if local in {"startTime", "stopTime"}:
                provider[local] = node.text
            if local == "software" and node.get("version"):
                version = node.get("version")
            if local in {"qualityInformation", "qualityCheck", "qualityFlag", "onlineQualityCheck"}:
                qa.setdefault(local, []).append({"attributes": dict(node.attrib), "text": "".join(node.itertext()).strip()})
        start = window.get("Start") or provider.get("startTime")
        end = window.get("End") or provider.get("stopTime")
        if start is None or end is None:
            raise ValueError("provider acquisition interval missing")
        footprint = metadata.get("GeoFootprint")
        if footprint is None:
            for node in tree.iter():
                if node.tag.split("}")[-1] == "posList" and node.text:
                    numbers = [float(v) for v in node.text.split()]
                    coordinates = [[numbers[i + 1], numbers[i]] for i in range(0, len(numbers), 2)]
                    if coordinates[0] != coordinates[-1]:
                        coordinates.append(coordinates[0])
                    footprint = {"type": "Polygon", "coordinates": [coordinates]}
                    break
        if bbox is not None:
            _bbox_to_wkt(bbox)
            intersects = False
            for asset in path.glob("geodetic_*.nc"):
                grid = asset.stem.rsplit("_", 1)[-1]
                if grid == "tx":
                    continue
                with xr.open_dataset(asset) as geo:
                    lon, lat = geo[f"longitude_{grid}"].values, geo[f"latitude_{grid}"].values
                    intersects |= bool(np.any((lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])))
            if not intersects:
                return frame_from_records([])
        definitions = {}
        for asset in path.glob("*.nc"):
            with xr.open_dataset(asset, decode_cf=False) as ds:
                for name, value in ds.data_vars.items():
                    if "err" in name or "uncertainty" in name:
                        definitions[name] = {k: v for k, v in value.attrs.items() if k in {"long_name", "description", "units"}}
        title = target.title if target else path.name
        published_collection = re.search(r"_([0-9]{3})\.SEN3$", title)
        row = dict(time=start, valid_time=start, integration_start=start, integration_end=end,
                   footprint_geometry=footprint, support_kind="swath", platform=title[:3] if title[:3] in {"S3A", "S3B", "S3C", "S3D"} else None, instrument="SLSTR",
                   quantity="slstr_level_1_observation", value=None, units=None,
                   unc_value=None, unc_status="unknown", unc_definition=json.dumps(definitions) if definitions else None,
                   source="sentinel-3-slstr-l1", source_agency="European Union / ESA / EUMETSAT",
                   source_url=target.source_url if target else None,
                   retrieved_at=raw.retrieved_at if isinstance(raw, SLSTRResult) else None,
                   algorithm_version=attrs.get("processorVersion") or version,
                   collection_version=attrs.get("baselineCollection") or (published_collection[1] if published_collection else None), qa=qa,
                   product_id=target.product_id if target else None, product_title=title,
                   provider_metadata=metadata, native_data_path=str(path))
        return frame_from_records([row])


class _ReaderKey(dict):
    """Minimal Satpy reader identifier; avoids assigning scene-derived attrs."""
    def __init__(self, channel, stripe, view, calibration):
        super().__init__(name=channel, stripe=SimpleNamespace(name=stripe),
                         view=SimpleNamespace(name="nadir" if view == "n" else "oblique"), calibration=calibration)

    def to_dict(self):
        return {key: getattr(value, "name", value) for key, value in self.items()}


def _native_array(value, grid, asset):
    """Only native image rows/columns share dimensions across provider files.

    Detector, integrator, uncertainty and orphan dimensions belong to their
    source asset; identical provider names never imply identical supports.
    """
    dimensions = {d: f"{d}_{grid}" if d in {"rows", "columns"} else f"{d}__{Path(asset).stem}"
                  for d in value.dims}
    value = value.load().rename(dimensions)
    value.attrs.update(provider_file=asset,
                       provider_dimensions={renamed: original for original, renamed in dimensions.items()})
    return value


def _copy_native(output, source, grid, window, *, asset):
    output.attrs.setdefault("provider_global_attributes", {})[asset] = deepcopy(source.attrs)
    for name, value in source.data_vars.items():
        if window:
            value = value.isel({d: s for d, s in window.items() if d in value.dims})
        value = _native_array(value, grid, asset)
        value.attrs["provider_variable"] = name
        packing = {k: value.encoding[k] for k in ("scale_factor", "add_offset", "_FillValue") if k in value.encoding}
        if packing:
            value.attrs["provider_packing"] = packing
        if np.issubdtype(value.dtype, np.datetime64):
            value.attrs["provider_time_units"] = value.encoding.get("units")
        if name in output:
            raise ValueError(f"duplicate provider variable {name} in {asset}")
        output[name] = value
