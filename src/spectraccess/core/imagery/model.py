"""Small, serialisable vocabulary for native imagery and its descriptors.

Structured attributes are JSON strings so netCDF4 groups and xarray's Zarr
writer preserve the same contract. Optional absent fields remain unstated.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import xarray as xr

from spectraccess.core.assumptions import AssumptionRecord

SCHEMA_VERSION = "0.1.0"
VALUE_BASES = {
    "published", "evaluated_from_provider_model", "scene_nominal",
    "interpolated_from_provider_grid", "unknown",
}
UNCERTAINTY_STATUSES = {"provided", "derived", "prior", "unknown"}
QUANTITIES = {
    "counts", "radiance", "toa_reflectance", "surface_reflectance",
    "brightness_temperature", "derived",
}
DESCRIPTORS = {
    "time", "sun_zenith", "sun_azimuth", "view_zenith", "view_azimuth",
    "latitude", "longitude", "footprint_longitude", "footprint_latitude",
    "detector_index",
}


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def describe(array, *, basis, uncertainty_status="unknown", source=None,
             assumptions=(), **attrs):
    """Label a value without promoting its uncertainty or inferring a basis."""
    if basis not in VALUE_BASES:
        raise ValueError(f"unsupported value basis: {basis}")
    if uncertainty_status not in UNCERTAINTY_STATUSES:
        raise ValueError(f"unsupported uncertainty status: {uncertainty_status}")
    result = array.copy(deep=False)
    result.attrs = dict(array.attrs, basis=basis, uncertainty_status=uncertainty_status, **attrs)
    if source is not None:
        result.attrs["source"] = str(source)
    if assumptions:
        result.attrs["assumptions"] = _json([
            a.to_dict() if isinstance(a, AssumptionRecord) else a for a in assumptions
        ])
    return result


def unknown(reason, *, units=None):
    """An explicit unknown descriptor; NaN never stands for a nominal value."""
    attrs = {"reason": reason}
    if units is not None:
        attrs["units"] = units
    return describe(xr.DataArray(np.nan), basis="unknown", **attrs)


def measurement_group(layers, *, primary, geometry_kind, processing_chain,
                      provider_level=None, **attrs):
    """Construct one native-grid group with independently described layers.

    Layer names are quantity names. Descriptors and QA arrays can be added to
    the returned Dataset. No resampling or uncertainty estimation occurs.
    """
    if primary not in layers:
        raise ValueError("primary quantity must name a layer")
    result = xr.Dataset(layers, attrs=dict(attrs))
    result.attrs.update(
        group_kind="measurement", primary_quantity=primary,
        stage=_json({"quantity": primary, "geometry_kind": geometry_kind,
                     "processing_chain": processing_chain}),
        quantity_layers=_json(list(layers)),
    )
    if provider_level is not None:
        result.attrs["provider_level"] = provider_level
    for name in layers:
        result[name].attrs["quantity"] = name
    return result


def imagery(groups, *, spectral_responses=None, **attrs):
    """Build a DataTree: /measurements/<native grid>, /spectral_responses/<entry>."""
    if not hasattr(xr, "DataTree"):
        raise ImportError("imagery requires spectraccess[imagery] (xarray>=2024.10)")
    nodes = {"/": xr.Dataset(attrs=dict(attrs, imagery_schema_version=SCHEMA_VERSION))}
    nodes.update({f"/measurements/{name}": ds for name, ds in groups.items()})
    nodes.update({f"/spectral_responses/{name}": ds
                  for name, ds in (spectral_responses or {}).items()})
    tree = xr.DataTree.from_dict(nodes)
    validate(tree)
    return tree


def _decode(attrs, name, errors, path):
    try:
        return json.loads(attrs[name])
    except (KeyError, TypeError, ValueError):
        errors.append(f"{path}: missing or invalid {name}")
        return None


def validate(tree, *, raise_errors=True):
    """Check known contract fields. Accept unknown additional fields.

    Returns error strings, or raises ValueError (default). Does not compute
    raster values. Absent optional descriptors mean unstated, not scene mean.
    """
    errors = []
    if not str(tree.attrs.get("imagery_schema_version", "")).startswith("0."):
        errors.append("root: missing or unsupported imagery_schema_version")
    groups = 0
    for node in tree.subtree:
        ds = node.to_dataset(inherit=False)
        path = node.path
        is_measurement = path.startswith("/measurements/")
        if is_measurement:
            groups += 1
            stage = _decode(ds.attrs, "stage", errors, path)
            layers = _decode(ds.attrs, "quantity_layers", errors, path)
            primary = ds.attrs.get("primary_quantity")
            if not isinstance(layers, list) or not layers or primary not in layers:
                errors.append(f"{path}: invalid quantity layers/primary")
                layers = []
            if not isinstance(stage, dict):
                stage = {}
            if stage.get("quantity") != primary or primary not in QUANTITIES:
                errors.append(f"{path}: stage quantity must describe primary layer")
            if stage.get("geometry_kind") not in {"map_grid", "sensor_geometry_with_lookup"}:
                errors.append(f"{path}: invalid stage geometry_kind")
            chain = stage.get("processing_chain")
            if not isinstance(chain, list):
                errors.append(f"{path}: stage needs processing_chain (empty allowed for raw cubes)")
            else:
                for step in chain:
                    if not isinstance(step, dict) or not all(
                        key in step for key in ("kind", "actor", "processor", "parameters")
                    ):
                        errors.append(f"{path}: incomplete processing step")
            reference = ds.get(primary) if isinstance(primary, str) else None
            for name in layers:
                if name not in ds or name not in QUANTITIES:
                    errors.append(f"{path}: missing or invalid layer {name}")
                    continue
                arr = ds[name]
                if not arr.attrs.get("units"):
                    errors.append(f"{path}/{name}: units required")
                if reference is not None and (arr.dims != reference.dims or arr.shape != reference.shape):
                    errors.append(f"{path}/{name}: layers must share native pixels")
                if ds.attrs.get("sensor") == "MSI" and name == "radiance":
                    if arr.attrs.get("basis") != "evaluated_from_provider_model":
                        errors.append(f"{path}/radiance: MSI radiance must be evaluated")
                if arr.attrs.get("conversion"):
                    conversion = _decode(arr.attrs, "conversion", errors, path + "/" + name)
                    if not isinstance(conversion, dict) or not all(
                        key in conversion for key in ("solar_spectrum", "sun_angle_convention")
                    ):
                        errors.append(f"{path}/{name}: conversion needs solar and sun provenance")
            for name, arr in ds.variables.items():
                if name not in layers and name not in DESCRIPTORS and "basis" not in arr.attrs:
                    continue
                if arr.attrs.get("basis") not in VALUE_BASES:
                    errors.append(f"{path}/{name}: descriptor needs basis")
                if arr.attrs.get("uncertainty_status") not in UNCERTAINTY_STATUSES:
                    errors.append(f"{path}/{name}: descriptor needs uncertainty_status")
                if "assumptions" in arr.attrs:
                    assumptions = _decode(arr.attrs, "assumptions", errors, path + "/" + name)
                    if not isinstance(assumptions, list):
                        errors.append(f"{path}/{name}: assumptions must be a list")
                    else:
                        from spectraccess.core.assumptions import AssumptionBasis
                        for a in assumptions:
                            try:
                                AssumptionRecord(a["assumption_id"], a["statement"],
                                                 AssumptionBasis(a["basis"]), a["resolves_when"])
                            except (KeyError, TypeError, ValueError):
                                errors.append(f"{path}/{name}: invalid assumption")
                if reference is not None and name in DESCRIPTORS:
                    for dim in arr.dims:
                        if dim not in reference.dims and dim not in {"detector", "vertex"}:
                            errors.append(f"{path}/{name}: descriptor cannot broadcast over {dim}")
        if path.startswith("/spectral_responses/"):
            representation = ds.attrs.get("representation")
            if representation not in {"tabulated", "parametric", "unknown"}:
                errors.append(f"{path}: invalid SRF representation")
            if "band" not in ds.coords:
                errors.append(f"{path}: SRF requires band keys")
            for name, arr in ds.data_vars.items():
                if arr.attrs.get("basis") not in VALUE_BASES:
                    errors.append(f"{path}/{name}: SRF descriptor needs basis")
                if arr.attrs.get("uncertainty_status") not in UNCERTAINTY_STATUSES:
                    errors.append(f"{path}/{name}: SRF descriptor needs uncertainty_status")
            if representation == "tabulated" and not {"wavelength", "response"} <= set(ds.variables):
                errors.append(f"{path}: tabulated SRF needs wavelength and response")
            if representation == "parametric" and not (
                {"centre", "fwhm"} <= set(ds.variables)
                and all(k in ds.attrs for k in ("shape", "shape_basis", "width_definition"))
            ):
                errors.append(f"{path}: parametric SRF needs centre, width and shape basis")
            if any(dim in ds.dims for dim in ("column", "detector")) and not (
                ds.attrs.get("key_to_pixel_map") or ds.attrs.get("assumptions")
            ):
                errors.append(f"{path}: keyed SRF needs pixel map or assumption")
    if not groups:
        errors.append("no measurement groups")
    if errors and raise_errors:
        raise ValueError("invalid imagery:\n" + "\n".join(errors))
    return errors


def site_box(tree, bounds):
    """Crop whole native pixels intersecting (west, south, east, north).

    Map-grid readers supply pixel-corner footprints; sensor readers must
    supply raw-pixel footprints. No GLT resampling or support weights. A raw
    bounding rectangle can include pixels outside the requested box.
    """
    west, south, east, north = bounds
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bounds must be ordered lon/lat, without antimeridian crossing")
    result = tree.copy(deep=False)
    for node in result.subtree:
        if not node.path.startswith("/measurements/"):
            continue
        ds = node.to_dataset(inherit=False)
        if not {"footprint_longitude", "footprint_latitude"} <= set(ds):
            raise ValueError("site_box requires raw native pixel footprints")
        lon, lat = ds.footprint_longitude, ds.footprint_latitude
        keep = ((lon.max("vertex") >= west) & (lon.min("vertex") <= east)
                & (lat.max("vertex") >= south) & (lat.min("vertex") <= north))
        slices = {}
        for dim in keep.dims:
            other = [d for d in keep.dims if d != dim]
            indices = np.flatnonzero(keep.any(other).values if other else keep.values)
            slices[dim] = slice(int(indices[0]), int(indices[-1]) + 1) if indices.size else slice(0, 0)
        node.dataset = ds.isel(slices)
    return result


def write_netcdf(tree, path: str | Path):
    """Validate and write netCDF4 groups. Existing files are replaced."""
    validate(tree)
    tree.to_netcdf(path, engine="netcdf4")


def open_netcdf(path: str | Path):
    """Open lazily; caller owns close(). Reject malformed known fields."""
    tree = xr.open_datatree(path, engine="netcdf4", decode_times=False)
    try:
        validate(tree)
    except BaseException:
        tree.close()
        raise
    return tree
