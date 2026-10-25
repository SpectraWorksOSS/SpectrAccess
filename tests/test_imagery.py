"""Contract conformance, malformed descriptions and native I/O (synthetic)."""

import json

import numpy as np
import pytest
import xarray as xr

from spectraccess.core.imagery import (
    describe, imagery, measurement_group, validate, write_netcdf, open_netcdf,
    site_box, unknown,
)


def synthetic(architecture="staggered"):
    dims = ("band", "line", "column")
    values = xr.DataArray(np.arange(24.).reshape(2, 3, 4), dims=dims,
                          coords={"band": ["A", "B"], "line": range(3), "column": range(4)})
    group = measurement_group({"counts": describe(values, basis="published", units="DN")},
        primary="counts", geometry_kind="sensor_geometry_with_lookup", processing_chain=[])
    if architecture == "staggered":
        time = xr.DataArray(np.arange(6.).reshape(2, 3), dims=("band", "line"),
                            coords={"band": ["A", "B"], "line": range(3)})
    else:
        time = xr.DataArray(np.arange(3.), dims="line")
    group["time"] = describe(time, basis="published", units="s")
    lon, lat = np.meshgrid(np.arange(4.), np.arange(3.))
    group["footprint_longitude"] = describe(xr.DataArray(np.stack([lon, lon+1, lon+1, lon], -1),
        dims=("line", "column", "vertex")), basis="published", units="degree")
    group["footprint_latitude"] = describe(xr.DataArray(np.stack([lat, lat, lat+1, lat+1], -1),
        dims=("line", "column", "vertex")), basis="published", units="degree")
    response = xr.Dataset({"centre": ("band", [450., 550.]), "fwhm": ("band", [8., 9.])},
        coords={"band": ["A", "B"]}, attrs={"representation": "parametric", "shape": "Gaussian",
        "shape_basis": "operator_declared", "width_definition": "half_max_fwhm"})
    for name in ("centre", "fwhm"):
        response[name] = describe(response[name], basis="published", units="nm")
    return imagery({"native": group}, spectral_responses={"native": response})


@pytest.mark.parametrize("architecture", ["staggered", "grating"])
def test_broadcast_same_consumer(architecture):
    tree = synthetic(architecture)
    ds = tree["/measurements/native"].to_dataset()
    values, time = xr.broadcast(ds.counts, ds.time)
    assert time.dims == values.dims == ("band", "line", "column")
    assert validate(tree) == []
    if architecture == "staggered":
        assert time[1, 0, 0] != time[0, 0, 0]
    else:
        assert time[1, 0, 0] == time[0, 0, 0]


@pytest.mark.parametrize("architecture", ["staggered", "grating"])
def test_netcdf_and_xarray_zarr_roundtrip(tmp_path, architecture):
    tree = synthetic(architecture)
    tree.attrs["future_field"] = "allowed"
    path = tmp_path / "imagery.nc"
    write_netcdf(tree, path)
    with open_netcdf(path) as reopened:
        xr.testing.assert_identical(tree, reopened)
    zarr = tmp_path / "imagery.zarr"
    tree.to_zarr(zarr, consolidated=False)
    with xr.open_datatree(zarr, engine="zarr", consolidated=False) as reopened:
        assert validate(reopened) == []
        xr.testing.assert_identical(tree, reopened)


@pytest.mark.parametrize("field", ["stage", "quantity_layers", "primary_quantity"])
def test_missing_group_contract_rejected(field):
    tree = synthetic()
    del tree["/measurements/native"].attrs[field]
    with pytest.raises(ValueError):
        validate(tree)


@pytest.mark.parametrize("field", ["basis", "uncertainty_status"])
def test_descriptor_needs_both_axes(field):
    tree = synthetic()
    del tree["/measurements/native/time"].attrs[field]
    with pytest.raises(ValueError, match=field):
        validate(tree)


def test_s2_published_radiance_rejected():
    tree = synthetic()
    ds = tree["/measurements/native"].to_dataset()
    ds["radiance"] = describe(ds.counts.copy(), basis="published", units="W m-2 sr-1 um-1")
    ds.attrs.update(sensor="MSI", quantity_layers=json.dumps(["counts", "radiance"]))
    tree["/measurements/native"].dataset = ds
    with pytest.raises(ValueError, match="MSI radiance must be evaluated"):
        validate(tree)


def test_keyed_srf_needs_map_or_assumption():
    tree = synthetic()
    node = tree["/spectral_responses/native"]
    node.dataset = node.to_dataset().expand_dims(detector=[1, 2])
    with pytest.raises(ValueError, match="pixel map or assumption"):
        validate(tree)
    node.attrs["key_to_pixel_map"] = "/measurements/native/detector_index"
    assert validate(tree) == []


def test_unknown_and_unstated_remain_different():
    tree = synthetic()
    assert "sun_zenith" not in tree["/measurements/native"]
    tree["/measurements/native/sun_zenith"] = unknown("no provider model")
    assert tree["/measurements/native/sun_zenith"].attrs["basis"] == "unknown"
    assert validate(tree) == []


def test_site_box_keeps_whole_native_pixels_and_original_indices():
    tree = synthetic("grating")
    crop = site_box(tree, (1.1, .1, 2.9, 1.9))
    group = crop["/measurements/native"].to_dataset()
    assert group.counts.shape == (2, 2, 2)
    np.testing.assert_array_equal(group.column, [1, 2])
    assert group.footprint_longitude.min() == 1
    assert group.footprint_longitude.max() == 3
    assert tree["/measurements/native/counts"].shape == (2, 3, 4)
    assert validate(crop) == []


def test_site_box_refuses_missing_footprints():
    tree = synthetic()
    node = tree["/measurements/native"]
    node.dataset = node.to_dataset().drop_vars("footprint_longitude")
    with pytest.raises(ValueError, match="footprints"):
        site_box(tree, (0., 0., 1., 1.))
